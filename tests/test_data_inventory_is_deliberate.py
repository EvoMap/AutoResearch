"""The public release starts without maintainer research data."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def tracked_data() -> list[str]:
    return subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "data/"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()


def test_public_release_contains_no_tracked_research_data() -> None:
    assert tracked_data() == []


def test_runtime_examples_live_outside_the_data_namespace() -> None:
    examples = sorted(path for path in (REPO / "examples").rglob("*") if path.is_file())

    assert examples, "the public release needs at least one synthetic example"
    assert all(path.is_relative_to(REPO / "examples") for path in examples)
