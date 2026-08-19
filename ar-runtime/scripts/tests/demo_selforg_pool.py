#!/usr/bin/env python3
"""自组织 worker 池端到端演示（协议级，无 LLM）。

场景：一个典型科研 DAG——plan_gate → 6 个独立消融实验 → join_analysis，
join 之后由引擎自己接上裁决链：critic → 无记忆盲审 → close。
每个消融单元做"真实工作"（写 results/<unit>.json + 模拟计算耗时 3s）。

演示三件事：
 D1 串行基线：1 个 worker 顺序抢完（≈ 老 next-prompt 模式的墙钟）
 D2 自组织池：4 个 worker 自己抢任务，就绪面宽时自动并行，收敛到 join 时自动串行
 D3 自愈：1 个 worker 抢到单元后立刻崩溃(不告知任何人)，租约过期后其余 worker 收回重做

critic.md、producer receipt 和 blind_review.md 在真实工作流里由 MCP 写入或登记，这里是桩
证据：引擎仍走同一条裁决路径。所以这个 demo 证明的是队列跑得到 close，不是评审本身有
质量。

时长由 AR_DEMO_WORK_SECONDS 控制。默认 3s 是给归档证据用的；测试拿它做协议 gate 时调小，
那种运行的墙钟数字没有意义（开销盖过工作），只看跑不跑得到底。
"""

import json
import hashlib
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ENGINE = str(Path(__file__).resolve().parent.parent / "ar-workflow-engine.py")
WORK_SECONDS = float(os.environ.get("AR_DEMO_WORK_SECONDS", "3.0"))
# 非 run 单元只是过一道门，不做重活。
GATE_SECONDS = min(0.5, WORK_SECONDS)
# 崩溃演示里那份短租约：它一过期，单元就该被别的 worker 收回。
CRASH_LEASE_SECONDS = int(os.environ.get("AR_DEMO_CRASH_LEASE_SECONDS", "3"))

# 裁决型单元的产物。引擎读 critic.md 决定还有没有下一轮，读 blind_review.md 按分数裁
# close 还是修订。这里给的是通过线以上的分数，演示走的是「一轮跑完就收尾」那条主路径；
# 低分会触发一轮修订，是另一条，不在本 demo 范围内。
STUB_ARTIFACTS = {
    "critic.md": (
        "- verdict: finish_ok\n"
        "- required_next_focus: none\n"
        "- stop_reason: demo_pilot_answered_the_question\n"
    ),
    "blind_review.md": (
        "- n_reviews: 3\n"
        "- avg_rating: 7.4\n"
        "- decision: accept\n"
        "- calibration_gap: 0.5\n"
        "- top_weaknesses: none\n"
    ),
}


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, ENGINE, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"engine failed: {args}\n{proc.stdout}\n{proc.stderr}")
    return proc


def append_stub_critic_receipt(root: Path, unit: dict, artifact: Path) -> None:
    """Add deterministic producer evidence for this protocol-only demo."""
    events_path = root / "workflow_events.jsonl"
    events = (
        [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]
        if events_path.exists()
        else []
    )
    verdict = "finish_ok"
    critics = []
    for role, model, identity in (
        ("critic", "demo-model-a", "demo-identity-a"),
        ("critic_secondary", "demo-model-b", "demo-identity-b"),
    ):
        critics.append({
            "role": role,
            "model": model,
            "model_identity": identity,
            "status": "ok",
            "verdict": verdict,
            "response_sha256": hashlib.sha256(
                f"{role}:{unit['id']}:{unit.get('cycle', 0)}:{verdict}".encode()
            ).hexdigest(),
        })
    event = {
        "schema_version": 1,
        "seq": len(events) + 1,
        "previous_hash": events[-1]["event_hash"] if events else "",
        "kind": "critic_receipt",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unit": unit["id"],
        "cycle": int(unit.get("cycle", 0) or 0),
        "source": "ar-external-critic-mcp",
        "receipt": {
            "schema_version": 1,
            "request_id": str(uuid.uuid4()),
            "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "producer_pid": os.getpid(),
            "unit": unit["id"],
            "cycle": int(unit.get("cycle", 0) or 0),
            "artifact": "critic.md",
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "verdict": verdict,
            "critics": critics,
        },
    }
    event["event_hash"] = hashlib.sha256(
        json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def append_stub_review_receipt(root: Path, unit: dict, artifact: Path) -> None:
    """Add deterministic producer evidence for a protocol-only review gate."""
    events_path = root / "workflow_events.jsonl"
    events = (
        [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]
        if events_path.exists()
        else []
    )
    issued_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    receipt = {
        "schema_version": 1,
        "request_id": str(uuid.uuid4()),
        "issued_at": issued_at,
        "producer_pid": os.getpid(),
        "unit": unit["id"],
        "cycle": int(unit.get("cycle", 0) or 0),
        "artifact": "review.md",
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "reviewer": "gemini-mcp-tool",
        "model": "demo-review-model",
        "model_identity": "demo-review-identity",
        "blockers_count": 0,
    }
    event = {
        "schema_version": 1,
        "seq": len(events) + 1,
        "previous_hash": events[-1]["event_hash"] if events else "",
        "kind": "review_report_receipt",
        "at": issued_at,
        "unit": unit["id"],
        "cycle": int(unit.get("cycle", 0) or 0),
        "source": "ar-gemini-review-mcp",
        "receipt": receipt,
    }
    event["event_hash"] = hashlib.sha256(
        json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def prepare_demo_runtime(root: Path) -> None:
    (root / "results").mkdir(exist_ok=True)
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    (root / ".venv" / "pyvenv.cfg").write_text("home = demo\n", encoding="utf-8")
    entrypoint = root / "code" / "demo_run.py"
    entrypoint.parent.mkdir()
    entrypoint.write_text(
        "import argparse, json, os, time\n"
        "from pathlib import Path\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--stage', required=True)\n"
        "p.add_argument('--artifact-dir', required=True)\n"
        "p.add_argument('--run-log', required=True)\n"
        "a=p.parse_args()\n"
        "unit=Path(a.artifact_dir).name\n"
        "time.sleep(float(os.environ.get('AR_DEMO_WORK_SECONDS', '3.0')))\n"
        "metric=sum(ord(c) for c in unit) % 100 / 100\n"
        "artifact=Path(a.artifact_dir)\n"
        "artifact.mkdir(parents=True, exist_ok=True)\n"
        "(artifact / 'observation.json').write_text(json.dumps({'unit':unit,'metric':metric})+'\\n')\n"
        "results=Path(a.run_log).parent\n"
        "(results / f'{unit}.json').write_text(json.dumps({'unit':unit,'metric':metric}))\n"
        "with Path(a.run_log).open('a') as h: h.write(f'[{a.stage}] {unit} metric={metric}\\n')\n",
        encoding="utf-8",
    )


def make_dag_project(root: Path | None = None) -> Path:
    root = (root or Path(tempfile.mkdtemp(prefix="ar_selforg_demo_"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    prepare_demo_runtime(root)
    ablations = [f"ablation_{name}" for name in ["lr", "warmup", "seed", "prune_ratio", "layer_subset", "baseline"]]
    units = [{"id": "plan_gate", "type": "planning", "status": "pending"}]
    units += [
        {
            "id": a,
            "type": "run",
            "stage": "iteration",
            "status": "pending",
            "blocked_by": "plan_gate",
        }
        for a in ablations
    ]
    units.append({"id": "join_analysis", "type": "result-analysis", "status": "pending", "blocked_by": ablations})
    queue = {"mode": "autoresearch_loop", "current_cycle": 0, "max_cycles": 3, "units": units}
    queue_path = root / "workflow_queue.json"
    if queue_path.exists():
        raise FileExistsError(f"refusing to replace an existing queue: {queue_path}")
    queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2))
    return root


def do_unit_work(root: Path, unit: dict, worker_name: str) -> str:
    """模拟真实单元工作：run 单元写指标文件并耗时计算。"""
    if unit["type"] == "run":
        artifact_dir = root / "results" / "run_artifacts" / unit["id"]
        summary = artifact_dir / "summary.json"
        executed = run([
            "execute-run",
            "--project-root",
            str(root),
            "--unit",
            unit["id"],
            "--worker",
            worker_name,
            "--",
            str(root / ".venv" / "bin" / "python"),
            str(root / "code" / "demo_run.py"),
            "--stage",
            str(unit.get("stage") or "iteration"),
            "--artifact-dir",
            str(artifact_dir),
            "--run-log",
            str(root / "results" / "run.log"),
        ])
        execution = json.loads(executed.stdout)
        metric = json.loads(
            (root / "results" / f"{unit['id']}.json").read_text()
        )["metric"]
        summary.write_text(
            json.dumps({"unit": unit["id"], "metric": metric}) + "\n",
            encoding="utf-8",
        )

        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        receipt_dir = root / "results" / "run_receipts"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        artifacts = [
            {"path": str(path.relative_to(root)), "sha256": digest(path)}
            for path in sorted(artifact_dir.rglob("*"))
            if path.is_file()
        ]
        events = [
            json.loads(line)
            for line in (root / "workflow_events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        execution_event = next(
            event
            for event in reversed(events)
            if event.get("event_hash") == execution["execution_event_hash"]
        )
        (receipt_dir / f"{unit['id']}.json").write_text(
            json.dumps({
                "schema_version": 1,
                "unit": unit["id"],
                "cycle": int(unit.get("cycle", 0) or 0),
                "status": "completed",
                "exit_code": 0,
                "started_at": execution_event["started_at"],
                "finished_at": execution_event["finished_at"],
                "execution_event_hash": execution["execution_event_hash"],
                "artifacts": artifacts,
                "summary": {
                    "path": str(summary.relative_to(root)),
                    "sha256": digest(summary),
                },
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return f"metric={metric}"
    if unit["type"] == "review":
        artifact = root / "review.md"
        artifact.write_text(
            "---\n"
            f"unit: {unit['id']}\n"
            f"cycle: {int(unit.get('cycle', 0) or 0)}\n"
            "reviewer: gemini-mcp-tool\n"
            "model: demo-review-model\n"
            "model_identity: demo-review-identity\n"
            "blockers_count: 0\n"
            "---\n\nNo blockers.\n",
            encoding="utf-8",
        )
        append_stub_review_receipt(root, unit, artifact)
        return "reviewed"
    if unit["type"] == "result-analysis":
        pattern = "ablation_*.json" if unit["id"] == "join_analysis" else "run_*.json"
        metrics = [
            json.loads(path.read_text())["metric"]
            for path in sorted((root / "results").glob(pattern))
        ]
        expected = 6 if unit["id"] == "join_analysis" else 1
        assert len(metrics) >= expected, (
            f"analysis ran before its run units finished: {len(metrics)} < {expected}"
        )
        (root / "results" / "summary.json").write_text(
            json.dumps({"n_branches": len(metrics), "best": max(metrics)})
        )
        (root / "state.md").write_text(
            f"- key_findings: {len(metrics)} run units completed\n"
            "- next_focus: none\n"
            "- stop_reason: the demo join is complete\n"
        )
        return f"analyzed_{len(metrics)}_runs"
    time.sleep(GATE_SECONDS)
    # 要写哪份产物由单元自己带着（引擎追加裁决单元时写进 artifact 字段），不由这里按类型猜。
    artifact = unit.get("artifact")
    if artifact:
        prefix = ""
        if artifact == "critic.md":
            prefix = f"- unit: {unit['id']}\n- cycle: {int(unit.get('cycle', 0) or 0)}\n"
        artifact_path = root / artifact
        artifact_path.write_text(prefix + STUB_ARTIFACTS[artifact])
        if artifact == "critic.md":
            append_stub_critic_receipt(root, unit, artifact_path)
        return f"wrote_{artifact}"
    return "ok"


def write_back(root: Path, unit: dict, name: str, result: str) -> None:
    """回写走引擎给的出口，不在这里记哪些类型要裁决。

    result-analysis / critic / blind-review 三型单元的「完成」就是裁决本身，`complete`
    对它们退 6，返回里带着该跑的 `after-*` 命令（引擎的 ADJUDICATION 表是唯一判据）。
    照着那条命令原样执行，引擎哪天再加一型，这条路径不会再断一次——它上一次就是这么断的
    （#218 加门，#227 才发现 demo 从那天起跑不到底）。
    """
    proc = run([
        "complete", "--project-root", str(root), "--unit", unit["id"],
        "--worker", name, "--status", "done", "--result", result,
    ], check=False)
    if proc.returncode == 0:
        return
    verdict = json.loads(proc.stdout or "{}")
    if verdict.get("reason") != "adjudication_required":
        raise AssertionError(f"complete failed: {unit['id']}\n{proc.stdout}\n{proc.stderr}")
    command = shlex.split(verdict["command"])
    print(f"    [{name}] {unit['id']}: complete rejected → {command[2]}", flush=True)
    # 原样执行引擎交回来的命令行，顺带验一件事：它拿来就能跑。
    settled = subprocess.run(command, capture_output=True, text=True)
    if settled.returncode != 0:
        raise AssertionError(f"adjudication failed: {unit['id']}\n{settled.stdout}\n{settled.stderr}")
    # 盲审产物没到位时引擎会把单元退回 pending（status=pending），worker 会再抢一次同一个
    # 单元。demo 写了产物，出现这种结果说明产物或时序不对，就地失败比空转一整轮好找原因。
    outcome = json.loads(settled.stdout or "{}")
    if outcome.get("status") != "ok":
        raise AssertionError(f"adjudication did not settle {unit['id']}: {settled.stdout}")


def worker(root_str: str, name: str, crash_after_claim: bool = False, lease: int = 7200) -> int:
    """自组织 worker：抢→干→回写→再抢。空转时短轮询（等上游解锁或租约回收）。"""
    root = Path(root_str)
    done = 0
    while True:
        out = json.loads(
            run(["claim", "--project-root", root_str, "--worker", name, "--lease-seconds", str(lease)]).stdout
        )
        if out["status"] == "empty":
            counts = out["counts"]
            if counts["pending"] == 0 and counts["running"] == 0:
                return done
            time.sleep(0.4)
            continue
        unit = out["unit"]
        if crash_after_claim:
            print(f"    [{name}] claimed {unit['id']} then CRASHED (no release, no complete)", flush=True)
            return -1
        write_back(root, unit, name, do_unit_work(root, unit, name))
        done += 1


def assert_closed(root: Path) -> None:
    """收没收场由引擎判：close 单元 done、队列无 active、盲审产物齐，三个都成立才 exit 0。"""
    proc = run(["verify-close", "--project-root", str(root)], check=False)
    assert proc.returncode == 0, f"verify-close 说没收场：{proc.stdout}\n{proc.stderr}"


def remove_demo_project(root: Path) -> None:
    """Restore permissions on sealed evidence before deleting the demo workspace."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)
    shutil.rmtree(root)


# plan_gate + 6 消融 + join_analysis，join 之后引擎追加 critic、盲审、close 各一个。
TOTAL_UNITS = 11


def demo_serial() -> float:
    root = make_dag_project()
    start = time.time()
    n = worker(str(root), "solo")
    wall = time.time() - start
    assert n == TOTAL_UNITS and (root / "results" / "summary.json").exists()
    assert_closed(root)
    print(f"  D1 serial baseline: 1 worker, {n} units, wall-clock {wall:.1f}s")
    remove_demo_project(root)
    return wall


def demo_pool() -> float:
    root = make_dag_project()
    start = time.time()
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker, str(root), f"w{i}") for i in range(4)]
        per_worker = [f.result() for f in futures]
    wall = time.time() - start
    assert sum(per_worker) == TOTAL_UNITS, per_worker
    summary = json.loads((root / "results" / "summary.json").read_text())
    assert summary["n_branches"] == 6
    assert_closed(root)
    print(
        f"  D2 self-organizing pool: 4 workers, {sum(per_worker)} units, wall-clock {wall:.1f}s, "
        f"per-worker load {per_worker} (workers balanced themselves, join waited for all 6 branches)"
    )
    remove_demo_project(root)
    return wall


def demo_self_heal() -> None:
    root = make_dag_project()
    run(["complete", "--project-root", str(root), "--unit", "plan_gate", "--status", "done"])
    start = time.time()
    with ProcessPoolExecutor(max_workers=4) as pool:
        crasher = pool.submit(worker, str(root), "w_crash", True, CRASH_LEASE_SECONDS)
        time.sleep(0.5)
        rescuers = [pool.submit(worker, str(root), f"rescue{i}") for i in range(3)]
        assert crasher.result() == -1
        total = sum(f.result() for f in rescuers)
    wall = time.time() - start
    remaining = TOTAL_UNITS - 1
    assert total == remaining, f"rescuers should finish all {remaining} remaining units, got {total}"
    assert_closed(root)
    log = (root / "decisions.log").read_text()
    assert "lease_reclaimed" in log and "lost_worker=w_crash" in log
    reclaim_line = next(line for line in log.splitlines() if "lease_reclaimed" in line)
    print(f"  D3 self-heal: worker crashed holding a lease; pool reclaimed and finished all work in {wall:.1f}s")
    print(f"     decisions.log: {reclaim_line.split(' | ')[-1]}")
    remove_demo_project(root)


def provenance() -> str:
    """这份输出被当证据引用，所以它得自己说清是哪台机器、哪个版本跑出来的。"""
    try:
        head = subprocess.run(
            ["git", "-C", str(Path(ENGINE).parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        commit = head.stdout.strip() if head.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
    return (
        f"started_at={datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"commit={commit} python={platform.python_version()} "
        f"platform={platform.platform()} work_seconds={WORK_SECONDS}"
    )


def main() -> None:
    print("self-organizing worker pool demo (plan → 6 ablations → join → critic → blind review → close)")
    print(f"  {provenance()}")
    serial = demo_serial()
    pool = demo_pool()
    demo_self_heal()
    result = f"RESULT: wall-clock {serial:.1f}s → {pool:.1f}s ({serial / pool:.1f}x speedup), self-heal verified"
    if WORK_SECONDS < 1:
        # 免得有人把 gate 那次运行的倍数抄去引用。
        result += f"  [work_seconds={WORK_SECONDS}，开销盖过工作，这个倍数不作数]"
    print(result)


if __name__ == "__main__":
    main()
