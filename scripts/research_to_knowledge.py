#!/usr/bin/env python3
"""Generate a review draft with the optional GPT Researcher dependency."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


REPO = Path(__file__).resolve().parents[1]
UPSTREAM_REPOSITORY = "https://github.com/assafelovic/gpt-researcher"
UPSTREAM_COMMIT = "92bfc0388c5f7a03b6cb34eaf6ae14298a4b458e"
DRAFT_DIRECTORY = REPO / "workspaces" / "knowledge-drafts"
KNOWLEDGE_PROMPT = """Write a source-backed Markdown research draft for AutoResearch.

Use these exact section headings and put concrete bullet points under each one:

## Pain Points
## Consensus and Debates
## Common Misconceptions
## Active Directions
## Research Opportunities
## Experimental Protocol
## References

Distinguish sourced facts from your synthesis. Keep source URLs in References.
Do not claim that this draft has been reviewed or accepted into the knowledge base.
"""


class ResearchDependencyError(RuntimeError):
    """The optional research environment is not ready."""


def load_researcher_class():
    """Import the optional dependency only after the paid-network gate."""
    try:
        from gpt_researcher import GPTResearcher
    except ModuleNotFoundError as exc:
        if exc.name != "gpt_researcher":
            raise
        raise ResearchDependencyError(
            "GPT Researcher is not installed. Create a Python 3.11 environment and run "
            "`python -m pip install -r requirements-research.txt`."
        ) from exc
    return GPTResearcher


def _slug(topic: str) -> str:
    slug = re.sub(r"[^\w-]+", "-", topic.casefold(), flags=re.UNICODE).strip("-_")
    return slug[:80] or "research-draft"


def default_output_path(topic: str) -> Path:
    return DRAFT_DIRECTORY / f"{_slug(topic)}.md"


def _render_draft(topic: str, report: str) -> str:
    return (
        "<!-- DRAFT: review before copying any content into knowledge_base/. -->\n"
        f"<!-- generated-with: {UPSTREAM_REPOSITORY}@{UPSTREAM_COMMIT} -->\n\n"
        f"# Research draft: {topic}\n\n"
        f"{report.strip()}\n"
    )


def _write_new_file_atomically(output: Path, body: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def generate_draft(
    topic: str,
    output: Path,
    *,
    researcher_factory: Callable[..., Any] | None = None,
) -> Path:
    """Run one upstream report and publish it as a new, review-only draft."""
    topic = topic.strip()
    if not topic:
        raise ValueError("topic must not be empty")

    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing draft: {output}")

    factory = researcher_factory or load_researcher_class()
    researcher = factory(
        query=topic,
        report_type="research_report",
        report_source="web",
    )
    await researcher.conduct_research()
    report = await researcher.write_report(custom_prompt=KNOWLEDGE_PROMPT)
    if not isinstance(report, str) or not report.strip():
        raise RuntimeError("GPT Researcher returned an empty report")

    _write_new_file_atomically(output, _render_draft(topic, report))
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a GPT Researcher draft for human review."
    )
    parser.add_argument("topic", help="Research topic or question")
    parser.add_argument(
        "--output",
        type=Path,
        help="Draft path; defaults to workspaces/knowledge-drafts/<topic>.md",
    )
    parser.add_argument(
        "--confirm-paid-network",
        action="store_true",
        help="Confirm that this run may send network requests and incur model API charges",
    )
    return parser


def _configure_logging() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPO / "logs" / f"research_to_knowledge_{timestamp}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.confirm_paid_network:
        parser.error(
            "this command sends network requests and may incur model API charges; "
            "rerun with --confirm-paid-network after reviewing the topic and provider settings"
        )

    if sys.version_info < (3, 11):
        parser.error(
            "the optional GPT Researcher environment requires Python 3.11 or newer; "
            "keep it separate from the core AutoResearch environment"
        )

    log_path = _configure_logging()
    output = args.output or default_output_path(args.topic)
    try:
        result = asyncio.run(generate_draft(args.topic, output))
    except (FileExistsError, ResearchDependencyError, RuntimeError, ValueError) as exc:
        logging.error("Research draft failed: %s", exc)
        return 2

    logging.info("Research draft written for human review: %s", result)
    print("SUMMARY")
    print(f"upstream_commit={UPSTREAM_COMMIT}")
    print(f"draft={result}")
    print(f"log={log_path}")
    print("next=Review the draft, then manually copy accepted material into knowledge_base/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
