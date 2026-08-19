"""Run producers and consumers must share the engine-owned stage contract."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_coder_builds_one_stage_scoped_entrypoint() -> None:
    coder = (REPO / "ar-runtime/.claude/agents/ar-coder.md").read_text()

    assert "--stage pilot|main|iteration" in coder
    assert "--artifact-dir <当前 unit 的不可变目录>" in coder
    assert "--run-log <共享 append-only run.log>" in coder
    assert "一次进程只能执行传入的一个 stage" in coder


def test_reviewer_blocks_cross_stage_execution_before_run() -> None:
    reviewer = (REPO / "ar-runtime/.claude/agents/ar-gemini-reviewer.md").read_text()

    assert "一次调用同时执行 pilot 和 main" in reviewer
    assert "必须作为 blocker" in reviewer
    assert "当前 unit 的 \u0060--artifact-dir\u0060" in reviewer


def test_runner_uses_engine_owned_execution_and_project_venv() -> None:
    runner = (REPO / "ar-runtime/.claude/agents/ar-runner.md").read_text()

    assert "<project_root>/.venv/bin/python" in runner
    assert "execute-run" in runner
    assert "--stage <experiment_stage>" in runner
    assert "--artifact-dir <results_dir>/run_artifacts/<unit>" in runner
    assert "--run-log <results_dir>/run.log" in runner
    assert "execution_event_hash" in runner
    assert "conda run" not in runner
    assert "宿主 Python 只允许创建 venv 和运行 workflow engine" in runner


def test_coordinator_requires_the_engine_execution_receipt() -> None:
    coordinator = (REPO / "ar-runtime/.claude/skills/ar-coordinator/SKILL.md").read_text()

    assert "execute-run" in coordinator
    assert "execution_event_hash" in coordinator
    assert "一次 run unit 只能执行自己的 stage" in coordinator
