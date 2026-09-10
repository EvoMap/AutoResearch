from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

import idea_provenance

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "src" / "generate_project_dashboard.py"


def load_dashboard():
    spec = importlib.util.spec_from_file_location("project_dashboard_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = load_dashboard()
    projects = tmp_path / "data" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "PROJECTS_DIR", projects)
    return module


def write_project(
    dashboard,
    slug: str = "demo",
    *,
    queue: object | None = None,
) -> Path:
    root = dashboard.PROJECTS_DIR / slug
    root.mkdir(parents=True)
    root.joinpath("state.md").write_text(
        """# State (last update: 2026-08-12T00:00:00+08:00)

## project
- idea: Verify the dashboard contract
- slug: demo
- current_step: 3

## step_status
- step_0_init: status=done
- step_A_spawn: status=done
- step_1_plan: status=done
- step_2_code: status=done
- step_3_review: status=running
- step_3_fix: status=not_needed
- step_4_run: status=pending
- step_5_result_analysis: status=pending
- step_6_critic: status=pending
- step_Z_close: status=pending
""",
        encoding="utf-8",
    )
    if queue is not None:
        root.joinpath("workflow_queue.json").write_text(
            json.dumps(queue),
            encoding="utf-8",
        )
    return root


def write_provenance(root: Path, b_id: str | None, body: str = "Executable idea\n") -> None:
    root.joinpath("idea.md").write_text(body, encoding="utf-8")
    payload = {
        "schema_version": 1,
        "source_file": "data/ideas/source.txt",
        "source_sha256": "0" * 64,
        "idea_file": "idea.md",
        "idea_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "b_id": b_id,
        "origin": None,
    }
    payload["binding_sha256"] = idea_provenance.manifest_binding_sha256(payload)
    root.joinpath("idea_provenance.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


@pytest.mark.parametrize("requested", ["../outside", "/tmp/outside"])
def test_render_one_rejects_paths_outside_projects(dashboard, tmp_path: Path, requested: str) -> None:
    outside = dashboard.PROJECTS_DIR.parent / "outside"
    outside.mkdir()
    outside.joinpath("state.md").write_text("## project\n- idea: outside\n", encoding="utf-8")

    if requested.startswith("/"):
        requested = str(outside)

    with pytest.raises(SystemExit, match="project slug"):
        dashboard.render_one(requested)

    assert not outside.joinpath("dashboard.html").exists()


def test_render_one_rejects_a_project_symlink_to_an_outside_directory(dashboard, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath("state.md").write_text("## project\n- idea: outside\n", encoding="utf-8")
    dashboard.PROJECTS_DIR.joinpath("linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SystemExit, match="resolved path leaves"):
        dashboard.render_one("linked")

    assert not outside.joinpath("dashboard.html").exists()


def test_status_dot_escapes_the_complete_title_attribute(dashboard) -> None:
    rendered = dashboard.status_dot('x" onmouseover="alert(1)', "note")

    assert ' onmouseover="' not in rendered
    assert 'title="x&quot; onmouseover=&quot;alert(1) · note"' in rendered


def test_corrupt_workflow_queue_is_not_reported_as_an_empty_queue(dashboard) -> None:
    root = write_project(dashboard)
    root.joinpath("workflow_queue.json").write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match=r"workflow_queue\.json.*invalid JSON"):
        dashboard.load_project("demo")


def test_cli_reports_corrupt_state_without_a_traceback(
    dashboard,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = write_project(dashboard)
    root.joinpath("workflow_queue.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "demo"])

    assert dashboard.main() == 2
    error = capsys.readouterr().err
    assert "workflow_queue.json" in error
    assert "repair the queue" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("status", ["running", "failed", "needs_revision", "blocked"])
def test_first_non_terminal_stage_remains_current(dashboard, status: str) -> None:
    target = 4
    statuses = {
        key: (("done" if i < target else status) if i <= target else "pending", "")
        for i, (key, _, _) in enumerate(dashboard.STEP_ORDER)
    }

    assert dashboard.current_step_index(statuses) == target


def test_in_progress_index_uses_stage_name_without_a_percentage(dashboard) -> None:
    project = {
        "slug": "demo",
        "sections": {
            "project": {"idea": "Verify the dashboard contract", "current_step": "3"},
            "_last_update": "2026-08-12T00:00:00+08:00",
        },
        "units": [
            {"status": "done"},
            {"status": "done"},
            {"status": "running"},
        ],
        "step_status": {
            "step_0_init": ("done", ""),
            "step_A_spawn": ("done", ""),
            "step_1_plan": ("done", ""),
            "step_2_code": ("done", ""),
            "step_3_review": ("running", ""),
            "step_Z_close": ("pending", ""),
        },
    }

    summary = dashboard.summarize_for_index(project)
    rendered = dashboard.render_index_html([summary])

    assert summary["pct"] is None
    assert summary["stage_label"] == "Code review"
    assert "Code review" in rendered
    assert re.search(r'class="idx-pct">\d+%', rendered) is None


def test_closed_index_is_the_only_state_that_displays_100_percent(dashboard) -> None:
    project = {
        "slug": "done",
        "sections": {"project": {"idea": "Done"}, "_last_update": ""},
        "units": [{"status": "failed"}],
        "step_status": {"step_Z_close": ("done", "")},
    }

    summary = dashboard.summarize_for_index(project)
    rendered = dashboard.render_index_html([summary])

    assert summary["pct"] == 100
    assert "100%" in rendered


def test_only_filters_the_index_without_skipping_project_dashboards(dashboard) -> None:
    first = write_project(dashboard, "first")
    second = write_project(dashboard, "second")

    dashboard.render_all(only={"second"})

    index = dashboard.PROJECT_ROOT.joinpath("dashboard_index.html").read_text(encoding="utf-8")
    assert first.joinpath("dashboard.html").exists()
    assert second.joinpath("dashboard.html").exists()
    assert "data/projects/second/dashboard.html" in index
    assert "data/projects/first/dashboard.html" not in index


def test_only_rejects_unknown_projects(dashboard) -> None:
    write_project(dashboard, "known")

    with pytest.raises(SystemExit, match=r"unknown project.*missing.*known"):
        dashboard.render_all(only={"missing"})


def test_project_pages_link_to_both_dashboards(dashboard) -> None:
    write_project(dashboard)

    page = dashboard.render_project_html(dashboard.load_project("demo"))

    assert "../../../dashboard_index.html" in page
    assert "../../../kb_dashboard.html" in page


def test_project_and_index_interfaces_are_english(dashboard) -> None:
    write_project(dashboard)
    project = dashboard.load_project("demo")

    project_page = dashboard.render_project_html(project)
    index_page = dashboard.render_index_html([dashboard.summarize_for_index(project)])

    for page in (project_page, index_page):
        assert '<html lang="en">' in page
        assert re.search(r"[\u3400-\u9fff]", page) is None
    assert "Project overview" in project_page
    assert "Knowledge base" in project_page
    assert "Execution cycles" in project_page
    assert "Knowledge base" in index_page


def test_knowledge_domain_comes_from_project_provenance_not_the_project_slug(
    dashboard,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge = tmp_path / "knowledge_base"
    knowledge.mkdir()
    knowledge.joinpath("mllm_visual_tokens.md").write_text(
        "# Visual token management\n\nEvidence-based pruning.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard.b_library, "KNOWLEDGE_BASE_DIR", knowledge)
    root = write_project(dashboard, "unrelated-project-directory")
    write_provenance(root, "mllm_visual_tokens")

    page = dashboard.render_project_html(dashboard.load_project(root.name))

    assert "Knowledge source" in page
    assert "Visual token management" in page
    assert "Evidence-based pruning." in page


def test_knowledge_domain_does_not_follow_a_symlink_outside_the_library(
    dashboard,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge = tmp_path / "knowledge_base"
    knowledge.mkdir()
    outside = tmp_path / "private.md"
    outside.write_text("# Private\n\nDO_NOT_EXPORT\n", encoding="utf-8")
    knowledge.joinpath("mllm_visual_tokens.md").symlink_to(outside)
    monkeypatch.setattr(dashboard.b_library, "KNOWLEDGE_BASE_DIR", knowledge)
    root = write_project(dashboard)
    write_provenance(root, "mllm_visual_tokens")

    with pytest.raises(dashboard.DashboardDataError, match="knowledge document"):
        dashboard.render_project_html(dashboard.load_project("demo"))


def test_project_provenance_detects_a_changed_executable_idea(dashboard) -> None:
    root = write_project(dashboard)
    write_provenance(root, None)
    root.joinpath("idea.md").write_text("Changed after initialization\n", encoding="utf-8")

    with pytest.raises(dashboard.DashboardDataError, match="idea_sha256"):
        dashboard.load_project("demo")


def test_declared_project_provenance_cannot_silently_disappear(dashboard) -> None:
    root = write_project(dashboard)
    state = root.joinpath("state.md").read_text(encoding="utf-8")
    state = state.replace(
        "- slug: demo\n",
        "- slug: demo\n- idea_provenance: ./data/projects/demo/idea_provenance.json\n",
    )
    root.joinpath("state.md").write_text(state, encoding="utf-8")

    with pytest.raises(dashboard.DashboardDataError, match="declares idea_provenance"):
        dashboard.load_project("demo")


@pytest.mark.parametrize("missing", [True, False])
def test_cli_renders_an_empty_project_overview(dashboard, monkeypatch, capsys, missing):
    if missing:
        monkeypatch.setattr(dashboard, "PROJECTS_DIR", dashboard.PROJECT_ROOT / "absent" / "projects")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--all"])

    assert dashboard.main() == 0

    output = capsys.readouterr()
    assert "(0 projects)" in output.out
    assert output.err == ""
    page = (dashboard.PROJECT_ROOT / "dashboard_index.html").read_text(encoding="utf-8")
    assert "0 projects" in page
    if missing:
        assert not dashboard.PROJECTS_DIR.exists()


def test_missing_projects_directory_still_rejects_unknown_filter(dashboard, monkeypatch):
    monkeypatch.setattr(dashboard, "PROJECTS_DIR", dashboard.PROJECT_ROOT / "absent" / "projects")
    with pytest.raises(SystemExit, match=r"unknown project.*missing.*none"):
        dashboard.render_all(only={"missing"})


def test_projects_path_must_be_a_directory(dashboard, monkeypatch):
    invalid = dashboard.PROJECT_ROOT / "not-a-directory"
    invalid.write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(dashboard, "PROJECTS_DIR", invalid)
    with pytest.raises(NotADirectoryError):
        dashboard.render_all()
