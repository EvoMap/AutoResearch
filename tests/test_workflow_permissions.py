"""Every public workflow declares the least GITHUB_TOKEN permission it needs."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_pr_title_workflow_needs_no_token_permissions():
    workflow = (REPO / ".github" / "workflows" / "pr-title.yml").read_text(
        encoding="utf-8"
    )

    assert "\npermissions: {}\n" in workflow


def test_public_ci_only_reads_repository_contents_when_present():
    workflow_path = REPO / ".github" / "workflows" / "ci.yml"
    if not workflow_path.exists():
        return

    workflow = workflow_path.read_text(encoding="utf-8")
    assert "\npermissions:\n  contents: read\n" in workflow
