"""Keep the public README attribution to the lab accurate and easy to verify."""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LAB_LINE = 'Infinite Evolution Lab, <a href="https://evomap.ai">EvoMap</a>'


def test_readme_attributes_the_project_to_the_lab() -> None:
    readme = REPO.joinpath("README.md").read_text(encoding="utf-8")

    assert readme.count(LAB_LINE) == 1
    assert "Project Leaders:" not in readme
    assert "mailto:" not in readme


def test_lab_line_replaces_the_old_eyebrow_and_precedes_the_workflow() -> None:
    readme = REPO.joinpath("README.md").read_text(encoding="utf-8")

    assert "An EvoMap open-source project" not in readme
    assert readme.index(LAB_LINE) < readme.index("docs/diagrams/autoresearch-workflow.svg")
