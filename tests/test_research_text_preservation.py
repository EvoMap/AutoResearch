"""Research inputs and persisted evidence must not be character-truncated."""

from __future__ import annotations

import ast
import importlib
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def test_seed_scoring_receives_complete_titles(monkeypatch):
    title = "标题" * 100 + "标题尾部"
    seen = {}
    stub = types.ModuleType("llm_client")

    def call_role(role, prompt, **kwargs):
        seen["prompt"] = prompt
        return "[9]"

    stub.call_role = call_role
    monkeypatch.setitem(sys.modules, "llm_client", stub)
    daily = importlib.import_module("idea_generation")

    assert daily.score_titles([{"title": title}]) == [9]
    assert title in seen["prompt"]


def test_pro_judgment_receives_complete_summaries(monkeypatch):
    pipeline = importlib.import_module("pipeline_v4")
    summary = "摘要" * 300 + "摘要尾部"
    seen = {}

    def call_role(role, prompt, **kwargs):
        seen["prompt"] = prompt
        return "最终判定: 值得深入 —— 值得验证"

    monkeypatch.setattr(pipeline, "call_role", call_role)
    pipeline.final_pro_judgment([
        {"title": "t", "url": "u", "summary": summary, "source": "s"}
    ], top_k=1)

    assert summary in seen["prompt"]


def test_freshness_reference_keeps_complete_search_evidence(monkeypatch):
    freshness = importlib.import_module("idea_forge.freshness")
    title = "论文标题" * 30 + "标题尾部"
    abstract = "论文摘要" * 100 + "摘要尾部"
    monkeypatch.setattr(
        freshness,
        "search_arxiv_latest",
        lambda *a, **k: [{"published": "2026-08", "title": title, "abstract": abstract}],
    )

    reference = freshness.build_arxiv_reference(["old-model"])

    assert title in reference
    assert abstract in reference


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/collectors/arxiv_collector.py",
        "src/collectors/hf_papers_collector.py",
        "src/collectors/openreview_collector.py",
        "src/collectors/reddit_collector.py",
        "src/collectors/influential_voices.py",
        "src/collectors/paper_digest_collector.py",
        "src/collectors/rss_collector.py",
    ],
)
def test_collectors_do_not_slice_research_text(relative_path):
    source = (REPO / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    failures = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
            continue
        if not isinstance(node.slice.upper, ast.Constant) or not isinstance(node.slice.upper.value, int):
            continue
        value = ast.get_source_segment(source, node.value) or ""
        if any(field in value for field in ("summary", "abstract", "selftext")):
            failures.append(ast.get_source_segment(source, node))

    assert not failures, failures
