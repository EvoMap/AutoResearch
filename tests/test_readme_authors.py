"""Keep the public README author links accurate and easy to verify."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
EXPECTED_LINKS = {
    "Yiming Ren": "https://scholar.google.com/citations?user=ayf4nGIAAAAJ",
    "Xiang Liu": "mailto:liuxiang@evomap.ai",
    "Qumeng Sun": "mailto:sun@evomap.ai",
    "Xiao Zhang": "mailto:zhangxiao@evomap.ai",
    "Jiahao Li": "https://github.com/likaho991007-design",
    "Haoyang Zhang": "https://autogame-17.github.io/",
    "Junjie Wang": "https://wangjunjie-ai.github.io/",
}


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._href: str | None = None
        self._text: list[str] = []
        self.links: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links["".join(self._text).strip()] = self._href
            self._href = None
            self._text = []


def test_readme_names_each_author_and_project_leader() -> None:
    readme = REPO.joinpath("README.md").read_text(encoding="utf-8")
    parser = AnchorParser()
    parser.feed(readme)

    assert {name: parser.links.get(name) for name in EXPECTED_LINKS} == EXPECTED_LINKS
    assert "Project Leaders:" in readme
    assert "Infinite Evolution Lab, <a href=\"https://evomap.ai\">EvoMap</a>" in readme


def test_author_block_replaces_the_old_eyebrow_and_precedes_the_workflow() -> None:
    readme = REPO.joinpath("README.md").read_text(encoding="utf-8")

    assert "An EvoMap open-source project" not in readme
    assert readme.index(">Yiming Ren</a>") < readme.index("docs/diagrams/autoresearch-workflow.svg")
