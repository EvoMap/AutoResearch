from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "src" / "generate_kb_dashboard.py"


def load_dashboard():
    spec = importlib.util.spec_from_file_location("kb_dashboard_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = load_dashboard()
    knowledge_base = tmp_path / "knowledge_base"
    knowledge_base.mkdir()
    monkeypatch.setattr(module.b_library, "KNOWLEDGE_BASE_DIR", knowledge_base)
    return module


def test_load_documents_reuses_the_prompt_boundary(dashboard, tmp_path: Path) -> None:
    root = dashboard.b_library.KNOWLEDGE_BASE_DIR
    root.joinpath("real.md").write_text("# Real\n\nUseful content.\n", encoding="utf-8")
    root.joinpath("README.md").write_text("# Private instructions\n", encoding="utf-8")
    root.joinpath("TEMPLATE.md").write_text("# Template\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# Secret\n\nOutside content.\n", encoding="utf-8")
    root.joinpath("leak.md").symlink_to(outside)

    documents = dashboard.load_documents()

    assert [document["file"] for document in documents] == ["real.md"]


def test_render_page_escapes_document_content_and_has_no_mtime_claim(dashboard) -> None:
    root = dashboard.b_library.KNOWLEDGE_BASE_DIR
    root.joinpath("unsafe.md").write_text(
        '# <script>alert("title")</script>\n\n<img src=x onerror=alert(1)>\n',
        encoding="utf-8",
    )

    page = dashboard.render_page(dashboard.load_documents())

    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;" in page
    assert "&lt;img src=x" in page
    assert "Last updated" not in page


def test_rendered_interface_is_english(dashboard) -> None:
    root = dashboard.b_library.KNOWLEDGE_BASE_DIR
    root.joinpath("multimodal_systems.md").write_text(
        "# Multimodal systems\n\nGrounded design notes.\n",
        encoding="utf-8",
    )

    page = dashboard.render_page(dashboard.load_documents())

    assert '<html lang="en">' in page
    assert "Knowledge Library" in page
    assert "Project overview" in page
    assert "Multimodal &amp; Vision" in page
    assert re.search(r"[\u3400-\u9fff]", page) is None


def test_render_page_handles_an_empty_knowledge_base(dashboard) -> None:
    page = dashboard.render_page(dashboard.load_documents())

    assert "0 research documents" in page
    assert 'class="board empty-board"' in page
