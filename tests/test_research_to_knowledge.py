"""Optional GPT Researcher integration stays reviewable and fail-closed."""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "research_to_knowledge.py"
UPSTREAM_COMMIT = "92bfc0388c5f7a03b6cb34eaf6ae14298a4b458e"


def load_adapter():
    assert SCRIPT.is_file(), "the optional research adapter is missing"
    spec = importlib.util.spec_from_file_location("research_to_knowledge", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dependency_is_pinned_to_the_official_upstream_commit():
    requirement = (REPO / "requirements-research.txt").read_text(encoding="utf-8")

    assert requirement.strip() == (
        "gpt-researcher @ git+https://github.com/assafelovic/"
        f"gpt-researcher.git@{UPSTREAM_COMMIT}"
    )
    assert "EvoMap" not in requirement


def test_cli_refuses_network_and_paid_calls_without_explicit_confirmation():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "agent runtime safety"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--confirm-paid-network" in result.stderr
    assert "requirements-research.txt" not in result.stderr


def test_generate_uses_upstream_api_and_writes_a_review_draft_atomically(tmp_path):
    adapter = load_adapter()
    calls: dict[str, object] = {}

    class FakeResearcher:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        async def conduct_research(self):
            calls["conducted"] = True

        async def write_report(self, *, custom_prompt):
            calls["prompt"] = custom_prompt
            return "# Evidence\n\n## Common Misconceptions\n\n- One\n\n## Research Opportunities\n\n- Two\n"

    output = tmp_path / "drafts" / "runtime-safety.md"
    result = asyncio.run(
        adapter.generate_draft(
            "agent runtime safety",
            output,
            researcher_factory=FakeResearcher,
        )
    )

    assert result == output
    assert calls["init"] == {
        "query": "agent runtime safety",
        "report_type": "research_report",
        "report_source": "web",
    }
    assert calls["conducted"] is True
    assert "Common Misconceptions" in calls["prompt"]
    assert "Research Opportunities" in calls["prompt"]
    body = output.read_text(encoding="utf-8")
    assert "DRAFT" in body
    assert UPSTREAM_COMMIT in body
    assert "## Common Misconceptions" in body
    assert not list(output.parent.glob(".*.tmp"))


def test_generate_never_overwrites_an_existing_review_draft(tmp_path):
    adapter = load_adapter()
    output = tmp_path / "existing.md"
    output.write_text("keep\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        asyncio.run(
            adapter.generate_draft(
                "topic",
                output,
                researcher_factory=lambda **_: pytest.fail("must not call provider"),
            )
        )

    assert output.read_text(encoding="utf-8") == "keep\n"


def test_default_output_stays_in_the_ignored_review_directory():
    adapter = load_adapter()
    output = adapter.default_output_path("Agent Runtime / Safety")

    assert output.parent == REPO / "workspaces" / "knowledge-drafts"
    assert output.name == "agent-runtime-safety.md"
    ignored = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "--quiet", "--", output],
        check=False,
    )
    assert ignored.returncode == 0


def test_optional_research_environment_is_ignored():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    environment = REPO / ".venv-research" / "pyvenv.cfg"

    assert "python3.11 -m venv .venv-research" in readme
    ignored = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "--quiet", "--", environment],
        check=False,
    )
    assert ignored.returncode == 0
