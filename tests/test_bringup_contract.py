"""bringup.sh 是新机器上第一条被执行的命令，它自己得先靠得住。

它的价值全在「说清楚缺什么」。这里钉住三件会让它失效的事：缺凭证
却悄悄退 0、把 secret 写进日志、SUMMARY 少了操作者要贴回来的字段。

沙箱是搭出来的，不是拷整个仓库：脚本会在仓库里跑 `pytest tests/`，把这个文件拷进去就会
让它再 spawn 一次脚本，无限递归（第一版就是这么挂住的）。沙箱里放一个平凡用例，行为一样
而不会自我调用；`.venv` 用符号链接指回来，免得每次都装一遍依赖。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "bringup.sh"

# 脚本要读的最小文件集。少一个它就会在别的地方失败，测出来的就不是我们要测的东西。
NEEDED = [
    "scripts/bringup.sh",
    "scripts/find-bun.sh",  # bringup 问它 bun 在哪，不自己找
    "scripts/preflight.py",
    "src/proxy_contract.py",  # preflight 探测按契约走代理时要读它
    "src/roles.py",           # preflight 解析角色模型要读它
    "src/providers.py",       # 请求由它构造和发送
    "src/api_retry.py",       # provider 与采集器共用的重试策略
    "scripts/secret_scan.py",
    "scripts/secret_scan_allowlist.json",
    "scripts/check_public_knowledge.py",
    "config/providers.example.json",
    "config.example.json",
    "requirements.txt",
    ".env.example",
]

# SUMMARY 是给人原样贴回来的，少一行就要多问一轮。
SUMMARY_FIELDS = ["host", "commit", "version", "config", "elapsed", "log", "verdict"]

# 沙箱里跑一次脚本包含一次完整 pytest，给足余量但必须有上限：没有上限时递归 bug 会挂住
# 整个测试会话，而不是红一条。
RUN_TIMEOUT = 300


@pytest.fixture
def sandbox(tmp_path):
    fake = tmp_path / "repo"
    for rel in NEEDED:
        target = fake / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
    (fake / "tests").mkdir()
    (fake / "tests" / "test_placeholder.py").write_text(
        "def test_the_sandbox_has_something_to_run():\n    assert True\n", encoding="utf-8")
    # 让沙箱直接用当前跑测试的解释器，免得脚本自己建 venv 再装一遍依赖。
    #
    # 是 exec 包装而不是软链：软链到一个 venv 的 python，它会按软链所在位置推断自己的
    # venv 根，于是 site-packages 全丢，`import httpx` 失败，沙箱多出两个与被测行为
    # 无关的 fail。CI 上没有仓库的 .venv，直接链过去还会悬空。
    launcher = fake / ".venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    # 沙箱只拷了一小部分文件，而 allowlist 的条目全指向没拷进来的路径。扫描器把
    # 「匹配不到的豁免」算作失败（这是它的正确设计，陈旧豁免要浮出来），所以沙箱里
    # 给一份空的，否则 bringup 会多出一个与被测行为无关的 fail。
    (fake / "scripts" / "secret_scan_allowlist.json").write_text(
        '{"allow": []}', encoding="utf-8")
    (fake / "knowledge_base").mkdir()
    (fake / "knowledge_base" / "public_manifest.json").write_text(
        '{"version": 1, "files": []}', encoding="utf-8")
    return fake


def run(sandbox, env=None):
    return subprocess.run(
        ["bash", str(sandbox / "scripts" / "bringup.sh")],
        capture_output=True, text=True, timeout=RUN_TIMEOUT,
        env=env if env is not None else os.environ.copy(),
    )


def without_credentials():
    """把可能让 preflight 通过的变量摘掉，测的才是「缺凭证」这条路。"""
    blocked = ("OPENAI_", "ANTHROPIC_", "GEMINI_", "AZURE_", "GATEWAY_", "EVOMAP_", "GOOGLE_")
    return {k: v for k, v in os.environ.items() if not k.startswith(blocked)}


def test_the_script_is_executable_and_parses():
    assert os.access(SCRIPT, os.X_OK), "新机器上第一条命令不该还要先 chmod"
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_a_stray_argument_changes_nothing(sandbox):
    """零 flag 是它的契约。第一版在源码里找 `case "$1"`，命中的却是 note() 自己的参数。

    改成跑两遍比结论：多给一个参数，verdict 必须一样。断行为，不断源码长什么样。
    """
    env = without_credentials()
    plain = run(sandbox, env=env)
    with_arg = subprocess.run(
        ["bash", str(sandbox / "scripts" / "bringup.sh"), "--verbose"],
        capture_output=True, text=True, timeout=RUN_TIMEOUT, env=env)

    assert plain.returncode == with_arg.returncode
    assert "getopts" not in SCRIPT.read_text(encoding="utf-8")


def test_it_never_prints_a_credential_value(sandbox):
    """只报告哪个变量是空的。把值打出来，日志就成了 secret 的副本。"""
    secret = "sk-test-DO-NOT-LEAK-0123456789"
    (sandbox / ".env").write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")

    done = run(sandbox, env=without_credentials())

    assert secret not in done.stdout
    assert secret not in done.stderr
    for log in (sandbox / "logs").glob("*"):
        assert secret not in log.read_text(encoding="utf-8", errors="ignore"), log.name


def test_it_loads_the_env_file_so_a_run_inherits_it(sandbox):
    """仓里没有任何代码读 .env，所以这一步只有脚本自己做。不做的话变量到不了子进程。"""
    (sandbox / ".env").write_text("OPENAI_API_KEY=x\n", encoding="utf-8")

    assert "已加载 .env" in run(sandbox, env=without_credentials()).stdout


def test_a_missing_credential_is_a_nonzero_exit(sandbox):
    """没有凭证却退 0，操作者会以为可以开跑，几分钟后才在第一次调用上失败。"""
    done = run(sandbox, env=without_credentials())

    assert done.returncode == 1, f"缺凭证必须非零退出，实际 {done.returncode}"
    assert "还差 1 项" in done.stdout, "只该因为缺凭证而红，其余项要是绿的"
    assert "preflight" in done.stdout
    # 要给出最省的那条路，而不是把所有候选的变量并集甩给人看。用户反馈的「要配的
    # API 太多」有一半就是这个并集造成的印象。
    assert "OPENAI_BASE_URL" in done.stdout and "OPENAI_API_KEY" in done.stdout
    assert "最省" in done.stdout
    # 完整清单仍然要有，只是收在后面。
    assert re.search(r"涉及的全部变量：.*[A-Z_]{6,}", done.stdout)


def test_the_summary_is_written_to_its_own_file(sandbox):
    """贴回来的那段要能直接 cat，不能让人从带颜色码的整篇日志里挑。"""
    run(sandbox, env=without_credentials())

    summaries = list((sandbox / "logs").glob("bringup_summary_*.txt"))
    assert len(summaries) == 1
    text = summaries[0].read_text(encoding="utf-8")
    assert "\x1b[" not in text, "SUMMARY 里不能有颜色码"
    for field in SUMMARY_FIELDS:
        assert re.search(rf"^{field}\s", text, re.M), f"SUMMARY 少了 {field}"


def test_gpu_detection_reports_capability_without_blocking_cpu_experiments():
    """GPU availability belongs to the Idea, not the runtime itself."""
    body = SCRIPT.read_text(encoding="utf-8")
    gpu_block = body.split("command -v nvidia-smi")[1].split("# ----")[0]
    detected, missing = gpu_block.split("else", 1)
    assert 'note ok "gpu"' in detected
    assert 'note warn "gpu"' in missing
    assert 'note fail "gpu"' not in gpu_block
    assert not any(
        line.strip().startswith('note warn "gpu"') and line.rstrip().endswith(" exec")
        for line in gpu_block.splitlines()
    )
    assert "CPU 实验仍可运行" in missing
    assert "./scripts/ar-supervisor.sh <idea> <project>" in body
    assert "READY" in body and "idea_gpu_smoke.txt" not in body.split("READY", 1)[1]


def test_the_smoke_idea_states_what_makes_it_fail():
    """冒烟 idea 的成功标准必须是显卡上可观测的量，否则 CPU 跑完也能报成功。"""
    idea = (REPO / "examples" / "idea_gpu_smoke.txt").read_text(encoding="utf-8")
    assert "torch.cuda.is_available()" in idea
    assert "synchronize" in idea, "不同步测到的是下发时间，不是计算时间"
    assert "不要退化成" in idea, "要写明 CUDA 不可用时不许降级成 CPU 报成功"


def test_the_missing_variable_list_survives_every_separator():
    """preflight 用三种分隔符列变量，抽取要认全。

    `either (A or B + C) or D` 里的 `+` 表示「这几个要一起给」。第一版只按逗号和
    ` or ` 分，于是 `B + C` 整体留成一个 token，被 `^[A-Z_]+$` 过滤掉。只以这种
    形态出现的变量就永远不会被提示。
    """
    import subprocess

    line = ("needs either (GEMINI_VERTEX_SERVICE_ACCOUNT or GOOGLE_APPLICATION_CREDENTIALS "
            "+ GEMINI_VERTEX_PROJECT_ID) or GEMINI_API_KEY, GEMINI_MODEL")
    extractor = next(row for row in SCRIPT.read_text(encoding="utf-8").splitlines()
                     if "tr ','" in row or "tr ',+'" in row)
    assert "',+'" in extractor, "分隔符里少了 +"

    pipeline = ("sed 's/^needs //' | tr ',+' '\\n\\n' "
                "| sed 's/ or /\\n/g; s/[()]//g; s/^ *//; s/ *$//' "
                "| grep -E '^[A-Z][A-Z0-9_]+$'")
    got = subprocess.run(["bash", "-c", f"printf '%s' \"$1\" | {pipeline}", "_", line],
                         capture_output=True, text=True).stdout.split()

    assert "GEMINI_VERTEX_PROJECT_ID" in got, f"+ 后面的变量被丢了：{got}"
    assert "GOOGLE_APPLICATION_CREDENTIALS" in got


# ---- 两条轨道分开表态 ----

def test_the_summary_answers_both_tracks_separately(sandbox):
    """出 idea 和跑实验的门不一样，一条能跑另一条不能是常见状态。

    合成一句「verdict 可以跑」会把其中一半说错：一台没装 bun 的 GPU 机器会被告知去跑
    实验，而 bun 缺失只记 warn 不算 blocker。RunPod 上正好撞到这个反例。
    """
    done = subprocess.run(["bash", str(sandbox / "scripts" / "bringup.sh")],
                          capture_output=True, text=True, cwd=str(sandbox),
                          timeout=RUN_TIMEOUT)

    for track in ("出 idea", "跑实验"):
        line = [ln for ln in done.stdout.splitlines()
                if track in ln and ("READY" in ln or "BLOCKED" in ln)]
        assert line, f"{track} 没有明确的 READY / BLOCKED：\n{done.stdout[-800:]}"


def with_bun_at(sandbox, *, on_path=False, in_home=False, version="1.2.3-fake"):
    """跑一遍 bringup，机器上的 bun 只在指定的那些位置。

    PATH 是摘掉带 bun 的那几段而不是清空：脚本还要用 bash、git、date，把 PATH 清了测出来
    的就是别的东西了。HOME 也换成沙箱里的，否则跑测试的这台机器自己装没装 bun 会决定
    结果。这三条要分辨的正是这些状态。
    """
    home = sandbox / "fake-home"
    home.mkdir(exist_ok=True)
    path = os.pathsep.join(part for part in os.environ["PATH"].split(os.pathsep)
                           if not (Path(part) / "bun").exists())

    for where, wanted in ((sandbox / "fake-path", on_path),
                          (home / ".bun" / "bin", in_home)):
        if not wanted:
            continue
        where.mkdir(parents=True, exist_ok=True)
        binary = where / "bun"
        binary.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
        binary.chmod(0o755)
    if on_path:
        path = f"{sandbox / 'fake-path'}{os.pathsep}{path}"

    return subprocess.run(["bash", str(sandbox / "scripts" / "bringup.sh")],
                          capture_output=True, text=True, cwd=str(sandbox),
                          timeout=RUN_TIMEOUT,
                          env={**os.environ, "PATH": path, "HOME": str(home)})


def experiment_verdict(done):
    # 只认 verdict 那两行：gpu 的 warn 说明里也有「跑实验」三个字。
    lines = [ln for ln in done.stdout.splitlines()
             if "跑实验" in ln and ("READY" in ln or "BLOCKED" in ln)]
    assert lines, done.stdout[-600:]
    return lines[0]


def with_claude_at(sandbox, *, installed):
    """Run with a deterministic official CLI state, independent of the host."""
    system_path = "/usr/bin:/bin:/usr/sbin:/sbin"
    assert shutil.which("claude", path=system_path) is None
    if installed:
        binary_dir = sandbox / "fake-claude-bin"
        binary_dir.mkdir()
        binary = binary_dir / "claude"
        binary.write_text("#!/bin/sh\necho 2.1.232-fake\n", encoding="utf-8")
        binary.chmod(0o755)
        system_path = f"{binary_dir}{os.pathsep}{system_path}"

    return run(sandbox, env={**without_credentials(), "PATH": system_path})


def test_an_installed_claude_cli_is_reported_with_its_version(sandbox):
    done = with_claude_at(sandbox, installed=True)

    lines = [line for line in done.stdout.splitlines()
             if "claude" in line.lower() and "2.1.232-fake" in line]
    assert lines, done.stdout[-800:]


def test_a_missing_claude_cli_blocks_experiments_only(sandbox):
    done = with_claude_at(sandbox, installed=False)

    assert "npm install -g @anthropic-ai/claude-code" in done.stdout
    assert "BLOCKED" in experiment_verdict(done)
    assert done.returncode == 1, "缺 provider 凭证仍应是 idea 轨的唯一退出码来源"


def test_bun_on_path_is_reported_with_its_version(sandbox):
    done = with_bun_at(sandbox, on_path=True)

    assert [ln for ln in done.stdout.splitlines() if "bun" in ln and "1.2.3-fake" in ln], \
        done.stdout[-600:]


def test_bun_installed_but_off_path_is_not_reported_as_missing(sandbox):
    """两种状态对 MCP server 的后果一样，下一步完全不同。

    `ar-runtime/.mcp.json` 里写的是 `"command": "bun"`，Claude Code 按 PATH 找，所以
    装在 ~/.bun/bin 而不在 PATH 上，server 确实起不来，跑实验那条仍然 BLOCKED。但把它
    报成「没装」，操作者就会照着 https://bun.sh 再装一遍，安装器写的还是那个位置，结果
    一模一样。这条盯的就是那句话有没有说对。
    """
    done = with_bun_at(sandbox, in_home=True)

    bun_lines = [line for line in done.stdout.splitlines() if "bun" in line.lower()]
    assert any("不在 PATH 上" in line for line in bun_lines), done.stdout[-600:]
    assert not any("没装" in line for line in bun_lines), \
        "Bun 装了却被报成没装，下一步就指错了"
    assert "BLOCKED" in experiment_verdict(done)


def test_bun_nowhere_blocks_experiments_but_not_ideas(sandbox):
    """判据是这台机器上的真实结论，不是源码里有没有那个词。"""
    done = with_bun_at(sandbox)

    assert "没装" in done.stdout, done.stdout[-600:]
    assert "BLOCKED" in experiment_verdict(done)


def test_the_exit_code_follows_the_track_every_machine_needs():
    """没有显卡的笔记本是合法的出 idea 机器。让它每次 exit 1 是假红，而假红的门
    迟早被关掉，所以退出码只看「出 idea」那条。"""
    body = SCRIPT.read_text(encoding="utf-8")

    assert '[ "$BLOCKERS" -eq 0 ] || exit 1' in body
    assert 'EXEC_BLOCKERS" -eq 0 ] || exit' not in body, "跑实验不该决定退出码"


def test_the_dependency_check_keys_on_the_requirements_file(sandbox):
    """升级一个已有 clone 时，新增的依赖看不见；#132 就是这种情况。

    只探一个包的话，requirements 里新加的东西装没装都报「已装」，紧接着测试报
    ModuleNotFoundError。判据要能跟着 requirements.txt 变。
    """
    body = SCRIPT.read_text(encoding="utf-8")

    assert "requirements.sha256" in body, "要按 requirements 的指纹判断"
    assert body.index("REQ_STAMP") < body.index('note ok "deps"'), "指纹要在判定之前算出来"


# ---- 本地投影恢复 ----

def test_projection_recovery_does_not_require_an_activated_shell():
    body = SCRIPT.read_text(encoding="utf-8")

    assert ".venv/bin/python scripts/render_env.py --check" in body


def test_the_pasted_summary_says_which_version_this_clone_is_on():
    """升级类问题第一句要问的就是这个，而 commit hash 回答不了：贴回来的人不知道那个
    hash 落在哪一版之后。"""
    body = SCRIPT.read_text(encoding="utf-8")

    assert "describe --tags" in body


def test_a_venv_without_pip_is_named_as_such():
    """uv 建的 venv 里没有 pip。报「pip install 失败」会让人去查网络和源。"""
    body = SCRIPT.read_text(encoding="utf-8")

    assert "uv pip install" in body, "要有 uv 兜底"
    assert "没有 pip" in body, "要说清是哪一种失败"
