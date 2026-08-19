"""The critic binding contract must reach the producer that writes critic.md."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_critic_unit_and_cycle_reach_the_mcp_renderer() -> None:
    coordinator = (REPO / "ar-runtime/.claude/skills/ar-coordinator/SKILL.md").read_text()
    agent = (REPO / "ar-runtime/.claude/agents/ar-critic.md").read_text()
    mcp = (REPO / "ar-runtime/scripts/ar-external-critic-mcp.ts").read_text()

    task_prompt = section(coordinator, 'Task(subagent_type="ar-critic"', "critic 返回 JSON 后")
    tool_call = section(agent, "mcp__ar-external-critic__external_critic(", ")\n   ```")
    tool = section(mcp, "'external_critic',", "if (process.argv.includes('--blind-test'))")

    assert "unit: <当前 critic unit id>" in task_prompt
    assert "cycle: <当前 critic unit cycle>" in task_prompt
    assert 'unit="<workflow critic unit id>"' in tool_call
    assert "cycle=<workflow critic unit cycle>" in tool_call
    assert 'project_root="<absolute project root>"' in tool_call
    assert 'output="<project_root>/critic.md"' in tool_call
    assert "unit: z" in tool and "cycle: z" in tool
    assert "project_root: z" in tool and "output: z" in tool
    assert "persistCriticReport(" in tool
    assert "recordCriticReceipt(" in tool
    assert "producer receipt" in agent
    agent_tools = agent.split("---", 2)[1]
    assert "Write" not in agent_tools
    assert "coordinator 不得自行生成或覆盖这份文件" in coordinator
