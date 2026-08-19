"""Coordinator instructions must match the official CLI async Agent contract."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_coordinator_waits_for_notifications_and_never_synthesizes_worker_artifacts() -> None:
    coordinator = (REPO / "ar-runtime/.claude/skills/ar-coordinator/SKILL.md").read_text()

    assert "当前会话没有 `TaskOutput`" in coordinator
    assert coordinator.count("TaskOutput") == 1
    assert "同一 unit、同一角色最多一个 in-flight agent" in coordinator
    assert "不要用 Bash `sleep`" in coordinator
    assert "旧 agent 的迟到通知只记录为 stale" in coordinator
    assert "不要创建、覆盖或修补 `review.md`" in coordinator
    assert "`results/run_receipts/` 或 `results/run_artifacts/`" in coordinator
    assert "通知到达前不得检查或补写产物" in coordinator
    assert "receipt 必须逐项列出该 unit 不可变目录里的全部普通文件" in coordinator
    assert '--max-cycles "${AR_MAX_CYCLES:-3}"' in coordinator
    assert "所有 unit 终态、iteration、下一个 pending unit 都只通过 workflow engine 更新" in coordinator
    assert "写回 state.md/workflow_queue.json" not in coordinator
    assert "必须同步更新 `workflow_queue.json`" not in coordinator
    assert "先把 iteration 修正" not in coordinator
    assert "直接编辑 workflow_queue.json" not in coordinator
