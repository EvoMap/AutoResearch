"""Runtime contracts for the real supervisor entry point.

The engine is the only completion authority. Timeouts and signals must reap the
whole Claude process group before sealing evidence. Monitor ownership is bound
to an exact absolute project root. Manifest creation fails closed.

Tests drive the real shell entry point with fake runners and temporary projects.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]

SUPERVISOR = REPO / "ar-runtime" / "scripts" / "ar-supervisor.sh"
ENGINE = REPO / "ar-runtime" / "scripts" / "ar-workflow-engine.py"
DEMO = REPO / "ar-runtime" / "scripts" / "tests" / "demo_selforg_pool.py"


def make_project(tmp_path: Path, units: list[dict]) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "workflow_queue.json").write_text(json.dumps({"units": units}))
    return root


def initialize_engine_authority(root: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ENGINE), "init", "--project-root", str(root)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def fake_claude(tmp_path: Path, body: str) -> Path:
    exe = tmp_path / "fake-claude"
    exe.write_text(f"#!/bin/bash\n{body}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def stubborn_claude(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_pid_file = tmp_path / "fake-claude.pid"
    child_pid_file = tmp_path / "fake-claude-child.pid"
    child = tmp_path / "fake-claude-child"
    child.write_text(
        "#!/bin/bash\n"
        f"echo $$ > {child_pid_file}\n"
        "trap '' TERM\n"
        "while :; do sleep 1; done\n")
    child.chmod(child.stat().st_mode | stat.S_IEXEC)
    claude = fake_claude(
        tmp_path,
        '[ "$1" = "--version" ] && { echo fake-1.0; exit 0; }\n'
        f"echo $$ > {fake_pid_file}\n"
        "trap '' TERM\n"
        f"{child} &\n"
        "wait")
    return claude, fake_pid_file, child_pid_file


def deterministic_coordinator(tmp_path: Path, root: Path) -> Path:
    exe = tmp_path / "deterministic-coordinator"
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import importlib.util\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--version':\n"
        "    print('deterministic-coordinator 1.0')\n"
        "    raise SystemExit(0)\n"
        f"spec = importlib.util.spec_from_file_location('selforg_demo', {str(DEMO)!r})\n"
        "demo = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(demo)\n"
        f"root = Path({str(root)!r})\n"
        "assert root.joinpath('workflow_queue.json').exists()\n"
        "demo.prepare_demo_runtime(root)\n"
        "completed = demo.worker(str(root), 'deterministic')\n"
        "assert completed > 0, completed\n"
        "demo.assert_closed(root)\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def run_supervisor(tmp_path: Path, root: Path, claude: Path,
                   max_restarts: str = "1", attempt_timeout: str = "30s",
                   extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    idea = tmp_path / "idea.txt"
    idea.write_text("noop")
    env = dict(os.environ, CLAUDE_BIN=str(claude), AR_SUPERVISOR_RETRY_SLEEP="1")
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SUPERVISOR), str(idea), str(root), max_restarts, attempt_timeout],
        capture_output=True, text=True, env=env, timeout=120,
    )


def process_table() -> dict[int, tuple[int, str]]:
    proc = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,command="], capture_output=True, text=True, check=True)
    table = {}
    for line in proc.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) >= 2:
            table[int(fields[0])] = (int(fields[1]), fields[2] if len(fields) == 3 else "")
    return table


def descendant_processes(parent_pid: int) -> dict[int, str]:
    table = process_table()
    descendants = {}
    frontier = [parent_pid]
    while frontier:
        parent = frontier.pop()
        children = [pid for pid, (ppid, _) in table.items() if ppid == parent]
        frontier.extend(children)
        descendants.update({pid: table[pid][1] for pid in children})
    return descendants


def ancestor_processes(pid: int, stop_pid: int) -> set[int]:
    table = process_table()
    ancestors = set()
    while pid in table and table[pid][0] != stop_pid:
        pid = table[pid][0]
        ancestors.add(pid)
    return ancestors


def kill_processes(pids: set[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def failing_python_shim(tmp_path: Path, command: str, rc: int) -> Path:
    shim_dir = tmp_path / f"shim-{command}"
    shim_dir.mkdir()
    python_shim = shim_dir / "python3"
    python_shim.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "../scripts/ar_run_manifest.py" ] && [ "$2" = "{command}" ]; then\n'
        f"  echo injected-manifest-{command}-failure >&2\n"
        f"  exit {rc}\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n')
    python_shim.chmod(python_shim.stat().st_mode | stat.S_IEXEC)
    return shim_dir


def stopping_monitor_writes_shim(tmp_path: Path) -> Path:
    shim_dir = tmp_path / "monitor-python-shim"
    shim_dir.mkdir()
    python_shim = shim_dir / "python3"
    python_shim.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "{REPO / "ar-runtime" / "scripts" / "ar-gemini-monitor.py"}" ]; then\n'
        "  shift\n"
        "  while [ $# -gt 0 ]; do\n"
        "    if [ \"$1\" = \"--project-root\" ]; then project_root=\"$2\"; break; fi\n"
        "    shift\n"
        "  done\n"
        "  trap 'sleep 0.2; echo stopped >> \"$project_root/results/notifications.log\"; exit 0' TERM\n"
        "  while :; do sleep 1; done\n"
        "fi\n"
        f'if [ "$1" = "{REPO / "ar-runtime" / "scripts" / "ar-workflow-engine.py"}" ]; then\n'
        "  sleep 0.5\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n')
    python_shim.chmod(python_shim.stat().st_mode | stat.S_IEXEC)
    return shim_dir


def test_drained_queue_with_forged_promise_is_not_done(tmp_path):
    root = make_project(tmp_path, [
        {"id": "spawn_agents", "status": "done"},
        {"id": "blind_review_x", "type": "blind-review", "status": "done"},
    ])
    initialize_engine_authority(root)
    (root / "decisions.log").write_text(
        "2026-08-12T00:00:00Z | step=Z | event=autoresearch_done | promise=AUTORESEARCH_DONE\n")
    claude = fake_claude(tmp_path, "exit 0")  # 每次 attempt 立即退出，什么都不修

    proc = run_supervisor(tmp_path, root, claude)

    assert proc.returncode == 2, (
        "队列排空 + coordinator 伪造的 promise 不构成完成；"
        f"supervisor 应耗尽重试后 exit 2，实际 {proc.returncode}\n{proc.stdout}{proc.stderr}")


def test_stale_promise_from_an_earlier_run_is_not_done(tmp_path):
    root = make_project(tmp_path, [
        {"id": "spawn_agents", "status": "pending"},
    ])
    initialize_engine_authority(root)
    (root / "decisions.log").write_text(
        "old run leftover: <promise>AUTORESEARCH_DONE</promise>\n")
    claude = fake_claude(tmp_path, "exit 0")

    proc = run_supervisor(tmp_path, root, claude)

    assert proc.returncode == 2


def test_supervisor_initializes_the_exact_max_cycles_before_the_coordinator(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    observed = tmp_path / "max-cycles.txt"
    claude = fake_claude(
        tmp_path,
        '[ "$1" = "--version" ] && { echo fake-1.0; exit 0; }\n'
        f'"{sys.executable}" "{REPO / "ar-runtime/scripts/ar-workflow-engine.py"}" '
        f'init --project-root "{root}" >/dev/null\n'
        f'printf "%s" "${{AR_MAX_CYCLES-unset}}" > "{observed}"\n'
        "exit 0",
    )

    proc = run_supervisor(
        tmp_path,
        root,
        claude,
        extra_env={"AR_MAX_CYCLES": "1"},
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert observed.read_text() == "1"
    assert json.loads((root / "workflow_queue.json").read_text())["max_cycles"] == 1


def test_supervisor_waits_indefinitely_for_print_mode_background_agents(tmp_path):
    root = make_project(tmp_path, [{"id": "spawn_agents", "status": "pending"}])
    observed = tmp_path / "background-wait-ceiling.txt"
    claude = fake_claude(
        tmp_path,
        '[ "$1" = "--version" ] && { echo fake-1.0; exit 0; }\n'
        f'printf "%s" "${{CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS-unset}}" > "{observed}"\n'
        "exit 0",
    )

    proc = run_supervisor(
        tmp_path,
        root,
        claude,
        extra_env={"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "600000"},
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert observed.read_text() == "0", (
        "print mode must not terminate a live reviewer at Claude Code's background wait ceiling")


def test_attempt_group_probe_eperm_does_not_escape_as_a_traceback(tmp_path):
    root = make_project(tmp_path, [{"id": "spawn_agents", "status": "pending"}])
    claude = fake_claude(
        tmp_path, '[ "$1" = "--version" ] && { echo fake-1.0; exit 0; }\nexit 0')
    shim_dir = tmp_path / "sitecustomize-shim"
    shim_dir.mkdir()
    (shim_dir / "sitecustomize.py").write_text(
        "import errno\n"
        "import os\n"
        "_real_killpg = os.killpg\n"
        "_raised = False\n"
        "def _killpg(pgid, sig):\n"
        "    global _raised\n"
        "    if sig == 0 and not _raised:\n"
        "        _raised = True\n"
        "        raise PermissionError(errno.EPERM, 'injected process-group probe failure')\n"
        "    return _real_killpg(pgid, sig)\n"
        "os.killpg = _killpg\n"
    )

    proc = run_supervisor(
        tmp_path,
        root,
        claude,
        extra_env={"PYTHONPATH": str(shim_dir)},
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert "PermissionError" not in proc.stderr


def test_engine_close_is_the_only_done(tmp_path):
    root = make_project(tmp_path, [
        {"id": "close_if_done", "type": "close", "status": "done"},
    ])
    (root / "results").mkdir()
    notifications = root / "results" / "notifications.log"
    notifications.write_text("initial\n")
    claude = fake_claude(tmp_path, "echo should-not-run; exit 1")
    shim_dir = stopping_monitor_writes_shim(tmp_path)

    proc = run_supervisor(
        tmp_path,
        root,
        claude,
        extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert proc.returncode == 0
    assert "should-not-run" not in proc.stdout, "close 已完成时不应再启动 coordinator"
    assert notifications.read_text().splitlines() == ["initial", "stopped"], (
        "precondition failed: the monitor did not perform its delayed shutdown write")
    attempts = sorted(root.glob("run_manifest.attempt-*.json"))
    assert len(attempts) == 1
    verified = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "ar_run_manifest.py"),
            "verify",
            "--project-root",
            str(root),
            "--attempt",
            str(attempts[0]),
        ],
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_fresh_project_reaches_verified_close_without_queue_recovery_edits(tmp_path):
    root = tmp_path / "fresh-project"
    root.mkdir()
    assert not (root / "workflow_queue.json").exists()
    coordinator = deterministic_coordinator(tmp_path, root)

    proc = run_supervisor(
        tmp_path,
        root,
        coordinator,
        extra_env={"AR_DEMO_WORK_SECONDS": "0.01"},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    verified_close = subprocess.run(
        [
            sys.executable,
            str(REPO / "ar-runtime" / "scripts" / "ar-workflow-engine.py"),
            "verify-close",
            "--project-root",
            str(root),
        ],
        capture_output=True,
        text=True,
    )
    assert verified_close.returncode == 0, verified_close.stdout + verified_close.stderr
    decisions = (root / "decisions.log").read_text()
    for event in (
        "event=after_result_analysis",
        "event=after_critic",
        "event=after_blind_review",
        "event=unit_marked unit=close_if_done status=done",
    ):
        assert event in decisions
    attempts = sorted(root.glob("run_manifest.attempt-*.json"))
    assert len(attempts) == 1
    verified_attempt = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "ar_run_manifest.py"),
            "verify",
            "--project-root",
            str(root),
            "--attempt",
            str(attempts[0]),
        ],
        capture_output=True,
        text=True,
    )
    assert verified_attempt.returncode == 0, verified_attempt.stdout + verified_attempt.stderr


def test_attempt_timeout_holds_without_gnu_timeout(tmp_path):
    root = make_project(tmp_path, [{"id": "spawn_agents", "status": "pending"}])
    claude, fake_pid_file, child_pid_file = stubborn_claude(tmp_path)
    poison_dir = tmp_path / "poison-timeout"
    poison_dir.mkdir()
    poison_marker = tmp_path / "poison-timeout-called"
    poison_timeout = poison_dir / "timeout"
    poison_timeout.write_text(
        "#!/bin/bash\n"
        f"touch {poison_marker}\n"
        "exit 99\n")
    poison_timeout.chmod(poison_timeout.stat().st_mode | stat.S_IEXEC)

    start = time.monotonic()
    proc = run_supervisor(
        tmp_path,
        root,
        claude,
        max_restarts="1",
        attempt_timeout="2s",
        extra_env={
            "AR_SUPERVISOR_FORCE_PY_TIMEOUT": "1",
            "AR_SUPERVISOR_TERM_GRACE": "1",
            "PATH": f"{poison_dir}{os.pathsep}{os.environ['PATH']}",
        },
    )
    elapsed = time.monotonic() - start
    fake_pid = int(fake_pid_file.read_text())
    child_pid = int(child_pid_file.read_text())
    survivors = {fake_pid, child_pid}.intersection(process_table())
    kill_processes(survivors)

    assert proc.returncode == 2
    assert not poison_marker.exists(), "forced Python fallback must not invoke GNU timeout"
    assert elapsed < 20, (
        f"attempt 应在 2s 超时后被终止（含收尾 sleep），实际耗时 {elapsed:.0f}s——"
        "说明超时兜底没有生效")
    assert not survivors, f"超时返回前必须回收 Claude 及后代，仍存活: {sorted(survivors)}"


def test_gnu_timeout_backend_keeps_the_normal_exit_contract(tmp_path):
    root = make_project(tmp_path, [{"id": "spawn_agents", "status": "pending"}])
    claude = fake_claude(
        tmp_path, '[ "$1" = "--version" ] && { echo fake-1.0; exit 0; }\nexit 0')
    shim_dir = tmp_path / "gnu-timeout-shim"
    shim_dir.mkdir()
    invocation = tmp_path / "timeout.argv"
    timeout = shim_dir / "timeout"
    timeout.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$@\" > {invocation}\n"
        "shift 2\n"
        "exec \"$@\"\n")
    timeout.chmod(timeout.stat().st_mode | stat.S_IEXEC)

    proc = run_supervisor(
        tmp_path,
        root,
        claude,
        extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    argv = invocation.read_text().splitlines()
    assert argv[0].startswith("--kill-after=")
    assert argv[1] == "30s"
    assert argv[2] == str(claude)


def spawn_fake_monitor(tmp_path: Path, project_root: Path) -> subprocess.Popen:
    monitor = tmp_path / "ar-gemini-monitor.py"
    if not monitor.exists():
        monitor.write_text("import time\nwhile True:\n    time.sleep(1)\n")
    return subprocess.Popen(["python3", str(monitor), "--project-root", str(project_root)])


def test_monitor_start_matches_the_exact_project_root(tmp_path):
    root = make_project(tmp_path, [{"id": "spawn_agents", "status": "pending"}])
    other_root = tmp_path / "proj-other"
    other_root.mkdir()
    other = spawn_fake_monitor(tmp_path, other_root)
    try:
        time.sleep(0.3)
        claude = fake_claude(tmp_path, "exit 0")

        proc = run_supervisor(tmp_path, root, claude)

        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "monitor started pid=" in proc.stdout, (
            "proj-other 的 monitor 不构成 proj 的已有 monitor；目标项目仍须启动自己的实例")
        assert other.poll() is None, "前缀相同的另一项目 monitor 不许被回收"
    finally:
        if other.poll() is None:
            other.kill()


def test_terminal_exit_reaps_this_projects_monitors(tmp_path):
    """终态回收 monitor 是 supervisor 的合同，不能只依赖 coordinator 记得 stop。

    对比 campaign 后本机实测累积了 14+ 个 PPID=1 的孤儿 ar-gemini-monitor（4-22 小时）：
    SKILL 把 stop 交给 coordinator，而会话可以死在那之前。三个负控齐上：别的项目的
    monitor、以及外审击穿过 basename 匹配用的「不同父目录、相同 basename」，都不许被误杀。
    """
    root = tmp_path / "proj_reap_gate"
    root.mkdir()
    (root / "workflow_queue.json").write_text(json.dumps({"units": [
        {"id": "close_if_done", "type": "close", "status": "done"},
    ]}))
    twin_dir = tmp_path / "another_parent"
    twin_dir.mkdir()
    (twin_dir / "proj_reap_gate").mkdir()

    mine = spawn_fake_monitor(tmp_path, root)
    other = spawn_fake_monitor(tmp_path, tmp_path / "someone_elses_project")
    twin = spawn_fake_monitor(tmp_path, twin_dir / "proj_reap_gate")
    try:
        time.sleep(0.3)
        claude = fake_claude(tmp_path, "exit 0")

        proc = run_supervisor(tmp_path, root, claude)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        time.sleep(0.5)
        assert mine.poll() is not None, "本项目的 monitor 应在终态被回收"
        assert other.poll() is None, "别的项目的 monitor 不许被误杀"
        assert twin.poll() is None, "相同 basename、不同父目录的项目不许被误杀"
    finally:
        for p in (mine, other, twin):
            if p.poll() is None:
                p.kill()


def test_sigterm_also_reaps_and_records_the_exit_code(tmp_path):
    """人工中断 / CI cancel / SSH 断开不是免检出口。

    实测旧版收到 SIGTERM 返回 143 后 monitor 仍存活；三个显式终态调用覆盖不了信号路径。
    trap 后 supervisor 自己拉起的 monitor 必须消失，attempt manifest 里落 143。
    """
    root = tmp_path / "proj_sigterm"
    root.mkdir()
    (root / "workflow_queue.json").write_text(json.dumps({"units": [
        {"id": "spawn_agents", "status": "pending"},
    ]}))
    idea = tmp_path / "idea.txt"
    idea.write_text("noop")
    # manifest start 会探 `$CLAUDE_BIN --version`；fake 必须秒回，否则 supervisor
    # 还没走到拉 monitor 就被测试的 SIGTERM 打断，测的就不是回收合同了。
    claude, fake_pid_file, child_pid_file = stubborn_claude(tmp_path)
    env = dict(
        os.environ,
        CLAUDE_BIN=str(claude),
        AR_SUPERVISOR_RETRY_SLEEP="1",
        AR_SUPERVISOR_TERM_GRACE="1",
    )

    proc = subprocess.Popen(
        ["bash", str(SUPERVISOR), str(idea), str(root), "1", "50s"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # manifest start（git diff + runner/bun 探版本）在慢机器上要几秒，monitor 在它之后
    # 才拉起；前提用轮询等，不赌一个固定的 sleep。
    monitor_up = ""
    for _ in range(80):
        time.sleep(0.25)
        got = subprocess.run(["pgrep", "-f", f"ar-gemini-monitor.*{root}"],
                             capture_output=True, text=True)
        if got.stdout.strip() and fake_pid_file.exists() and child_pid_file.exists():
            monitor_up = got.stdout.strip()
            break
    attempt_tree = descendant_processes(proc.pid)
    fake_pid = int(fake_pid_file.read_text()) if fake_pid_file.exists() else -1
    child_pid = int(child_pid_file.read_text()) if child_pid_file.exists() else -1
    wrapper_pids = ancestor_processes(fake_pid, proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=30)
        time.sleep(1)
        after = process_table()
        survivors = set(attempt_tree).intersection(after)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        kill_processes(set(attempt_tree).intersection(process_table()))

    assert proc.returncode == 143, proc.returncode
    assert monitor_up, "前提不成立：supervisor 没把 monitor 拉起来"
    assert fake_pid in attempt_tree, "前提不成立：未捕获 fake Claude PID"
    assert child_pid in attempt_tree, "前提不成立：未捕获 fake Claude 后代 PID"
    assert wrapper_pids, "前提不成立：未捕获 timeout wrapper PID"
    assert not survivors, (
        "supervisor 写 manifest 前必须 TERM、超时 KILL 并 wait 到 wrapper、Claude 及后代退出；"
        f"仍存活: {[(pid, attempt_tree[pid]) for pid in sorted(survivors)]}")

    attempts = sorted(root.glob("run_manifest.attempt-*.json"))
    assert attempts, "supervisor 起跑应写 attempt manifest"
    doc = json.loads(attempts[-1].read_text())
    assert doc.get("supervisor_exit_code") == 143, doc.get("supervisor_exit_code")


def test_manifest_start_failure_is_fail_closed(tmp_path):
    root = make_project(tmp_path, [
        {"id": "close_if_done", "type": "close", "status": "done"},
    ])
    legacy = root / "run_manifest.json"
    legacy.write_text('{"sentinel": "must-not-change"}\n')
    claude = fake_claude(tmp_path, "echo coordinator-must-not-run; exit 0")
    shim_dir = failing_python_shim(tmp_path, "start", 41)

    proc = run_supervisor(
        tmp_path,
        root,
        claude,
        extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert proc.returncode == 41, proc.stdout + proc.stderr
    assert "coordinator-must-not-run" not in proc.stdout
    assert legacy.read_text() == '{"sentinel": "must-not-change"}\n'
    assert not list(root.glob("run_manifest.attempt-*.json"))


def test_manifest_finish_failure_changes_a_successful_exit_to_failure(tmp_path):
    root = make_project(tmp_path, [
        {"id": "close_if_done", "type": "close", "status": "done"},
    ])
    claude = fake_claude(tmp_path, "echo coordinator-must-not-run; exit 0")
    shim_dir = failing_python_shim(tmp_path, "finish", 42)

    proc = run_supervisor(
        tmp_path,
        root,
        claude,
        extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert proc.returncode == 42, proc.stdout + proc.stderr
    assert "run manifest finish failed rc=42" in proc.stderr
    assert "coordinator-must-not-run" not in proc.stdout
    attempts = sorted(root.glob("run_manifest.attempt-*.json"))
    assert len(attempts) == 1
    assert "finished_at_utc" not in json.loads(attempts[0].read_text())
    monitors_after = subprocess.run(
        ["pgrep", "-f", f"ar-gemini-monitor.*{root}"], capture_output=True, text=True)
    assert not monitors_after.stdout.strip(), "manifest failure must not bypass monitor cleanup"


def test_a_second_invocation_gets_its_own_attempt_file(tmp_path):
    """事后补跑 supervisor 曾把起跑现场覆盖成 checkout 时刻——归档里的假起跑时间。

    每次 invocation 一个独占 attempt 文件；run_manifest.json 只归第一个 attempt。
    """
    root = make_project(tmp_path, [
        {"id": "close_if_done", "type": "close", "status": "done"},
    ])
    claude = fake_claude(tmp_path, "exit 0")

    first = run_supervisor(tmp_path, root, claude)
    assert first.returncode == 0
    original = (root / "run_manifest.json").read_text()
    first_attempts = sorted(root.glob("run_manifest.attempt-*.json"))
    assert len(first_attempts) == 1

    time.sleep(1.1)
    second = run_supervisor(tmp_path, root, claude)
    assert second.returncode == 0

    assert (root / "run_manifest.json").read_text().startswith(
        original.split('"finished_at_utc"')[0]), "第一个 attempt 的起跑现场不许被覆盖"
    assert len(sorted(root.glob("run_manifest.attempt-*.json"))) == 2, \
        "第二次 invocation 要有自己的 attempt 文件"
