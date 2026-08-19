from __future__ import annotations

import re
import struct
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ENGLISH = REPO / "README.md"
CHINESE = REPO / "README_CN.md"
PROJECT_MONITOR_IMAGE = "docs/images/autoresearch-project-monitor.png"
KNOWLEDGE_BASE_IMAGE = "docs/images/autoresearch-knowledge-base-board.png"
ARXIV_URL = "https://arxiv.org/abs/2608.17906"
REPORT_BADGE = (
    f'<a href="{ARXIV_URL}"><img alt="Report: arXiv:2608.17906" '
    'src="https://img.shields.io/badge/Report-arXiv%3A2608.17906-B31B1B"></a>'
)
CITATION = REPO / "CITATION.cff"
TAGLINE = "Insight In, Hallucination Out."
ENGLISH_LANGUAGE_SWITCH = (
    '<p align="center">\n'
    '  English &nbsp;·&nbsp; <a href="README_CN.md">简体中文</a>\n'
    "</p>"
)
CHINESE_LANGUAGE_SWITCH = (
    '<p align="center">\n'
    '  <a href="README.md">English</a> &nbsp;·&nbsp; 简体中文\n'
    "</p>"
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def numbered_sections(text: str) -> list[str]:
    return re.findall(r"^## ([1-9])\.", text, flags=re.MULTILINE)


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", data[16:24])


def test_default_readme_is_english_and_links_to_chinese() -> None:
    text = read(ENGLISH)

    assert not text.startswith("[中文](README_CN.md)")
    assert ENGLISH_LANGUAGE_SWITCH in text
    assert "## 1. Ways to Use AutoResearch" in text
    assert "## 1. 使用路径" not in text


def test_chinese_readme_links_back_to_english() -> None:
    text = read(CHINESE)

    assert not text.startswith("[English](README.md)")
    assert CHINESE_LANGUAGE_SWITCH in text
    assert "## 1. 使用路径" in text


def test_hero_and_diagrams_use_the_report_tagline() -> None:
    paths = (
        ENGLISH,
        CHINESE,
        REPO / "docs/diagrams/autoresearch-workflow.svg",
        REPO / "docs/diagrams/autoresearch-workflow-cn.svg",
    )

    for path in paths:
        text = read(path)
        assert TAGLINE in text, f"{path.name} is missing the report tagline"
        assert "Bring an Idea. Build the Evidence." not in text


def test_both_languages_link_the_report_and_keep_the_same_structure() -> None:
    english = read(ENGLISH)
    chinese = read(CHINESE)

    for text in (english, chinese):
        assert REPORT_BADGE in text
        assert text.count(ARXIV_URL) == 1
        assert "arXiv:2608.17906" in text
        assert "Report: <a" not in text
        assert "技术报告：<a" not in text
        assert numbered_sections(text) == [str(index) for index in range(1, 10)]


def test_each_language_uses_its_matching_workflow_diagram() -> None:
    english = read(ENGLISH)
    chinese = read(CHINESE)
    english_diagram = read(REPO / "docs/diagrams/autoresearch-workflow.svg")

    assert "(docs/diagrams/autoresearch-workflow.svg)" in english
    assert "(docs/diagrams/autoresearch-workflow-cn.svg)" in chinese
    assert not re.search(r"[\u3400-\u9fff]", english_diagram)


def test_both_languages_show_the_project_and_knowledge_dashboards() -> None:
    for path in (ENGLISH, CHINESE):
        text = read(path)
        assert text.count(f"]({PROJECT_MONITOR_IMAGE})") == 1
        assert text.count(f"]({KNOWLEDGE_BASE_IMAGE})") == 1
        assert ".venv/bin/python src/generate_kb_dashboard.py" in text

    for image in (PROJECT_MONITOR_IMAGE, KNOWLEDGE_BASE_IMAGE):
        assert png_dimensions(REPO / image) == (1600, 1100)


def test_github_citation_file_prefers_the_arxiv_report() -> None:
    citation = read(CITATION)
    preferred = citation.split("preferred-citation:", maxsplit=1)[1]

    assert citation.startswith("cff-version: 1.2.0\n")
    assert 'title: "AutoResearch"' in citation
    assert "type: software" in citation
    assert "license: Apache-2.0" in citation
    assert "repository-code: https://github.com/EvoMap/AutoResearch" in citation
    assert "type: article" in preferred
    assert 'title: "AutoResearch: Insight In, Hallucination Out"' in preferred
    assert ARXIV_URL in preferred
    for name in (
        "Yiming Ren",
        "Xiang Liu",
        "Qumeng Sun",
        "Xiao Zhang",
        "Jiahao Li",
        "Haoyang Zhang",
        "Junjie Wang",
    ):
        given, family = name.split()
        assert f'given-names: "{given}"' in preferred
        assert f'family-names: "{family}"' in preferred


def test_both_languages_preserve_the_runnable_entrypoints() -> None:
    required = (
        "bash scripts/bringup.sh",
        ".venv/bin/python scripts/preflight.py --live",
        ".venv/bin/python idea_generation.py",
        "scripts/ar-supervisor.sh",
        ".venv/bin/python src/generate_project_dashboard.py --all",
    )

    for path in (ENGLISH, CHINESE):
        text = read(path)
        missing = [command for command in required if command not in text]
        assert not missing, f"{path.name} is missing runnable entrypoints: {missing}"
