"""Check that published dependency declarations match imported modules."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Distribution name to import name.  Command-line tools are intentionally
# listed with None because they are exercised by CI rather than imported.
DISTRIBUTION_TO_MODULE = {
    "beautifulsoup4": "bs4",
    "openreview-py": "openreview",
    "google-auth": "google",
    "ruff": None,
    "pytest": None,
}

VENDORED_BY = {
    "botocore": "boto3",
    "jmespath": "boto3",
    "s3transfer": "boto3",
    "dateutil": "boto3",
    "urllib3": "boto3",
}


def requirements_text() -> str:
    files = [REPO / "requirements.txt", REPO / "requirements-research.txt"]
    return "\n".join(
        path.read_text(encoding="utf-8") for path in files if path.exists()
    )


def declared() -> list[str]:
    names = []
    for line in requirements_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
            assert match, f"无法识别依赖声明：{line}"
            names.append(match.group())
    return names


def test_the_optional_git_requirement_is_parsed_by_distribution_name():
    assert "gpt-researcher" in declared()


EXCLUDED = {
    "data",
    ".venv",
    ".venv-research",
    ".conda-env",
    "node_modules",
    "__pycache__",
    "worktrees",
}


def our_python_files() -> list[Path]:
    return [
        path
        for path in REPO.rglob("*.py")
        if not set(path.relative_to(REPO).parts) & EXCLUDED
    ]


def imported_modules() -> set[str]:
    found: set[str] = set()
    for path in our_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
    return found


def test_supported_research_environment_is_not_scanned(tmp_path, monkeypatch):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("import httpx\n", encoding="utf-8")
    dependency = tmp_path / ".venv-research" / "site-packages" / "legacy.py"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"# -*- coding: big5 -*-\n# \xa4@\xa8\xc7\xa4\xa4\xa4\xe5\n")
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    assert imported_modules() == {"httpx"}


def test_every_declared_dependency_is_imported():
    used = imported_modules()
    unused = []
    for name in declared():
        module = DISTRIBUTION_TO_MODULE.get(name, name.replace("-", "_"))
        if module and module not in used:
            unused.append(f"{name}（找的是 import {module}）")

    assert not unused, (
        "requirements.txt 声明了没人 import 的东西。删掉，或者在同一行注释里写清它为谁而在：\n  "
        + "\n  ".join(unused)
    )


def test_a_third_party_import_is_declared_or_documented_as_optional():
    text = requirements_text()
    local = {path.stem for path in our_python_files()}
    local |= {"src", "tests", "scripts", "idea_forge", "collectors", "config"}

    undeclared = sorted(
        imported_modules()
        - set(sys.stdlib_module_names)
        - local
        - {
            DISTRIBUTION_TO_MODULE.get(n, n.replace("-", "_"))
            for n in declared()
        }
    )

    for module in undeclared:
        if module in VENDORED_BY:
            assert VENDORED_BY[module] in text, (
                f"{module} 由 {VENDORED_BY[module]} 带进来，但后者没声明"
            )
            continue
        assert module in text, (
            f"{module} 被 import 但 requirements.txt 里既没声明也没提。"
            "可选依赖要在文件里写清怎么装、以及缺了会怎样。"
        )
