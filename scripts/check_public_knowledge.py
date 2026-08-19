#!/usr/bin/env python3
"""Verify that the published knowledge tree matches its reviewed manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MANIFEST_NAME = "public_manifest.json"


def load_manifest(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"manifest missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"manifest invalid JSON: {path}:{exc.lineno}:{exc.colno}"
        ) from exc

    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError(f"{path}: expected an object with version=1")
    files = payload.get("files")
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
        raise ValueError(f"{path}: files must be a list of relative paths")
    if len(files) != len(set(files)):
        raise ValueError(f"{path}: files contains duplicate entries")

    for item in files:
        relative = Path(item)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md":
            raise ValueError(f"{path}: invalid knowledge path: {item}")
    return files


def findings(root: Path) -> list[str]:
    knowledge = root / "knowledge_base"
    if not knowledge.is_dir():
        return [f"knowledge directory missing: {knowledge}"]

    try:
        listed = set(load_manifest(knowledge / MANIFEST_NAME))
    except ValueError as exc:
        return [str(exc)]

    actual = {
        path.relative_to(knowledge).as_posix()
        for path in knowledge.rglob("*.md")
        if path.is_file() or path.is_symlink()
    }
    problems = [f"listed file missing: {item}" for item in sorted(listed - actual)]
    problems.extend(f"unreviewed file: {item}" for item in sorted(actual - listed))

    for item in sorted(listed & actual):
        path = knowledge / item
        if path.is_symlink():
            problems.append(f"knowledge file must be a regular file: {item}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".",
                        help="repository or exported release root")
    args = parser.parse_args()
    root = Path(args.root).resolve()

    problems = findings(root)
    if problems:
        print("Public knowledge check failed:", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        print(
            f"Next step: review the file change, then update knowledge_base/{MANIFEST_NAME}.",
            file=sys.stderr,
        )
        return 1

    print("Knowledge base matches the public manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
