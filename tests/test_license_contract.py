from __future__ import annotations

import hashlib
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
APACHE_2_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


def test_public_tree_uses_the_official_apache_2_license() -> None:
    readme = REPO.joinpath("README.md").read_text(encoding="utf-8")
    license_sha256 = hashlib.sha256(REPO.joinpath("LICENSE").read_bytes()).hexdigest()

    assert license_sha256 == APACHE_2_SHA256
    assert "License: Apache-2.0" in readme
    assert "## 9. License" not in readme
    assert "版权归各贡献者所有" not in readme
    assert "不包含这些第三方项目的源码" not in readme
    assert "AG" + "PL" not in readme
    assert "Aff" + "ero" not in readme


def test_public_readme_has_a_software_citation_template() -> None:
    readme = REPO.joinpath("README.md").read_text(encoding="utf-8")
    citation = readme.split("```bibtex", maxsplit=1)[1].split("```", maxsplit=1)[0]
    citation_fields = (
        "@software{ren2026autoresearch,",
        "Yiming Ren",
        "Xiang Liu",
        "Qumeng Sun",
        "Xiao Zhang",
        "Jiahao Li",
        "Haoyang Zhang",
        "Junjie Wang",
        "title = {AutoResearch}",
        "year = {2026}",
        "url = {https://github.com/EvoMap/AutoResearch}",
    )

    assert "## 9. Citation" in readme
    positions = [citation.index(field) for field in citation_fields]
    assert positions == sorted(positions)
