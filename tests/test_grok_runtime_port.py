"""Grok harness copies of ar-runtime skills, agents, and workflows."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
GROK = REPO / ".grok"


def test_grok_skills_and_agents_exist() -> None:
    for name in (
        "ar-coordinator",
        "ar-experiment-runner",
        "ar-gpu-preflight",
        "ar-workspace-safety",
    ):
        assert (GROK / "skills" / name / "SKILL.md").is_file(), name

    for name in (
        "ar-planner",
        "ar-coder",
        "ar-subcoder",
        "ar-runner",
        "ar-gemini-reviewer",
        "ar-critic",
        "ar-blind-reviewer",
    ):
        assert (GROK / "agents" / f"{name}.md").is_file(), name

    assert (GROK / "workflows" / "ar-coordinator.rhai").is_file()
    assert (GROK / "workflows" / "ar-experiment-matrix.rhai").is_file()
    assert (GROK / "config.toml").is_file()
    assert (REPO / "ar-runtime" / ".grok" / "config.toml").is_file()


def test_grok_coordinator_uses_grok_harness_and_the_engine() -> None:
    skill = (GROK / "skills" / "ar-coordinator" / "SKILL.md").read_text(encoding="utf-8")
    workflow = (GROK / "workflows" / "ar-coordinator.rhai").read_text(encoding="utf-8")

    assert "spawn_subagent" in skill
    assert "resume_from" in skill
    assert "python \"$REPO/src/idea_provenance.py\" inspect" in skill
    assert "python \"$REPO/src/idea_provenance.py\" prepare" in skill
    assert "./scripts/ar-preflight-mcp.sh" in skill
    assert "execute-run" in skill
    assert "execution_event_hash" in skill
    assert "A run unit may only execute its own stage" in skill
    assert "TaskOutput" not in skill
    assert "/ralph-loop" not in skill
    assert "spawn_subagent" in skill
    assert "idea_artifact: <project_root>/idea.md" in skill
    assert "idea_provenance: <project_root>/idea_provenance.json" in skill

    assert 'name: "ar-coordinator"' in workflow
    assert "next-prompt" in workflow
    assert "ar-planner" in workflow
    assert "ar-blind-reviewer" in workflow
    assert "after-result-analysis" in workflow


def test_grok_reviewer_and_critic_do_not_write_producer_artifacts() -> None:
    reviewer = (GROK / "agents" / "ar-gemini-reviewer.md").read_text(encoding="utf-8")
    critic = (GROK / "agents" / "ar-critic.md").read_text(encoding="utf-8")
    coder = (GROK / "agents" / "ar-coder.md").read_text(encoding="utf-8")
    runner = (GROK / "agents" / "ar-runner.md").read_text(encoding="utf-8")

    def tools_line(text: str) -> str:
        for line in text.split("---", 2)[1].splitlines():
            if line.startswith("tools:"):
                return line
        return ""

    assert "write" not in tools_line(reviewer)
    assert "search_replace" not in tools_line(reviewer)
    assert "write" not in tools_line(critic)
    assert "ar-gemini-review__gemini_review" in reviewer
    assert "ar-external-critic__external_critic" in critic
    assert "--stage pilot|main|iteration" in coder
    assert "--artifact-dir <the immutable directory for the current unit>" in coder
    assert "--run-log <the shared append-only run.log>" in coder
    assert "A single process invocation may only execute the one stage it was given" in coder
    assert "<project_root>/.venv/bin/python" in runner
    assert "execute-run" in runner
    assert "execution_event_hash" in runner
    assert "subcoder_requests" in coder


def test_grok_mcp_config_points_at_runtime_servers() -> None:
    root = (GROK / "config.toml").read_text(encoding="utf-8")
    runtime = (REPO / "ar-runtime" / ".grok" / "config.toml").read_text(encoding="utf-8")

    assert "[mcp_servers.ar-gemini-review]" in root
    assert "[mcp_servers.ar-external-critic]" in root
    assert 'cwd = "ar-runtime"' in root
    assert "ar-gemini-review-mcp.ts" in root
    assert "ar-external-critic-mcp.ts" in root
    assert "./scripts/ar-gemini-review-mcp.ts" in runtime
    assert "./scripts/ar-external-critic-mcp.ts" in runtime
    assert "Bash(rm -rf *)" in root
