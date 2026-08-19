"""The review binding contract must reach the MCP producer of review.md."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_review_unit_and_cycle_reach_the_mcp_persistence_boundary() -> None:
    coordinator = (REPO / "ar-runtime/.claude/skills/ar-coordinator/SKILL.md").read_text()
    agent = (REPO / "ar-runtime/.claude/agents/ar-gemini-reviewer.md").read_text()
    mcp = (REPO / "ar-runtime/scripts/ar-gemini-review-mcp.ts").read_text()

    task_prompt = section(
        coordinator,
        'Task(subagent_type="ar-gemini-reviewer",\n     description="配置角色 MCP 审代码"',
        "**注意**:",
    )
    run_gate_prompt = section(
        coordinator,
        'Task(subagent_type="ar-gemini-reviewer",\n     description="Reviewer 判定 run 结果是否接受"',
        "| reviewer decision |",
    )
    tool_call = section(agent, "mcp__ar-gemini-review__gemini_review(", ")\n   ```")
    tool = section(mcp, "'gemini_review',", "if (process.argv.includes('--self-test'))")

    assert "unit: <当前 workflow review unit id>" in task_prompt
    assert "cycle: <当前 workflow cycle>" in task_prompt
    assert "idea_path: <project_root>/idea.md" in task_prompt
    assert "idea_path: <project_root>/idea.md" in run_gate_prompt
    assert "读取 `idea_path`" in agent
    assert "硬约束" in agent
    assert 'project_root="<project_root>"' in tool_call
    assert 'output="<project_root>/review.md"' in tool_call
    assert 'unit="<workflow review unit id>"' in tool_call
    assert "cycle=<workflow cycle>" in tool_call
    assert "persistReviewReport(" in tool
    agent_tools = agent.split("---", 2)[1]
    assert "Write" not in agent_tools
