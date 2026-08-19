"""Which candidates reach Pro judgment, and in what order.

Separate from test_forge_params, which is about how many.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "src" / "collectors"))


@pytest.fixture
def mixed_pipeline(monkeypatch):
    """A pool where the hottest posts are academic and sit last in concatenation order.

    pipeline_v4 builds its pool as `community_insightful + academic_insightful`, so
    an unranked cap takes community posts first no matter how cold they are.
    """
    module = importlib.import_module("pipeline_v4")

    posts = (
        [{"title": f"cold-community-{i}", "url": f"https://example.invalid/c{i}",
          "source": "reddit_ml", "source_type": "community",
          "num_comments": 1, "score": 1} for i in range(20)]
        + [{"title": f"hot-paper-{i}", "url": f"https://arxiv.invalid/{i}",
            "source": "arxiv", "source_type": "academic",
            "num_comments": 500, "score": 500} for i in range(5)]
    )
    monkeypatch.setattr(module, "collect_all_channels", lambda **kw: list(posts))
    monkeypatch.setattr(module, "filter_by_discussion_quality", lambda items, **kw: list(items))
    monkeypatch.setattr(module, "load_processed_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(module, "filter_already_processed", lambda items, *a, **kw: list(items))

    def split_filter(items, **kw):
        # The real one is called twice, once per source group.
        return list(items)

    monkeypatch.setattr(module, "llm_insight_filter", split_filter)

    judged = {}

    def fake_judgment(candidates, top_k=10):
        judged["titles"] = [c.get("title") for c in candidates]
        judged["sources"] = [c.get("source_type") for c in candidates]
        return []

    monkeypatch.setattr(module, "final_pro_judgment", fake_judgment)
    monkeypatch.setattr(module.Path, "mkdir", lambda *a, **kw: None)
    monkeypatch.setattr(module.json, "dump", lambda *a, **kw: None)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: __import__("io").StringIO())
    return module, judged


def test_a_capped_run_takes_the_hottest_not_the_first(mixed_pipeline) -> None:
    """With top_k below the community count, no paper reached Pro judgment."""
    module, judged = mixed_pipeline
    module.run_pipeline_v4(top_k=5)

    assert len(judged["titles"]) == 5
    assert all(t.startswith("hot-paper-") for t in judged["titles"]), judged["titles"]
    assert "academic" in judged["sources"]


def test_uncapped_run_is_also_ranked(mixed_pipeline) -> None:
    """Order still matters without a cap: a run that dies partway judges a prefix."""
    module, judged = mixed_pipeline
    module.run_pipeline_v4()

    assert len(judged["titles"]) == 25
    assert judged["titles"][:5] == [f"hot-paper-{i}" for i in range(5)]


def test_ranking_key_is_comments_weighted_over_score(mixed_pipeline) -> None:
    """num_comments * 2 + score, which is the key the original code used."""
    module, judged = mixed_pipeline
    posts = [
        {"title": "score-heavy", "url": "https://a.invalid", "source": "x",
         "source_type": "community", "num_comments": 0, "score": 100},
        {"title": "comment-heavy", "url": "https://b.invalid", "source": "y",
         "source_type": "community", "num_comments": 60, "score": 0},
    ]
    module.collect_all_channels = lambda **kw: list(posts)
    module.run_pipeline_v4(top_k=1)
    assert judged["titles"] == ["comment-heavy"], "60*2 beats 100"


def test_truncation_is_announced(mixed_pipeline, capsys) -> None:
    """A silent cap reads as "that was all we found"."""
    module, _ = mixed_pipeline
    module.run_pipeline_v4(top_k=5)
    out = capsys.readouterr().out
    assert "候选 25，预算上限 5，截断 20" in out


def test_no_truncation_notice_when_nothing_was_dropped(mixed_pipeline, capsys) -> None:
    module, _ = mixed_pipeline
    module.run_pipeline_v4(top_k=100)
    assert "截断" not in capsys.readouterr().out


def test_the_saved_list_is_ranked_too(mixed_pipeline, monkeypatch) -> None:
    """The saved JSON must carry the same order Pro judged."""
    module, _ = mixed_pipeline
    saved = {}
    monkeypatch.setattr(module.json, "dump", lambda obj, *a, **kw: saved.update(obj))

    module.run_pipeline_v4(top_k=5)

    titles = [p["title"] for p in saved["all_insightful"]]
    assert titles[:5] == [f"hot-paper-{i}" for i in range(5)], titles[:8]
