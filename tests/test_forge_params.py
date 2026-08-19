"""Tests for the two parameter defects in the idea pipeline.

Both were silent: --top-k did nothing, and a review budget too small for reasoning
models made those reviewers abstain from every idea without logging an error.
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "src" / "collectors"))


@pytest.fixture
def pipeline(monkeypatch):
    """pipeline_v4 with every network and model call stubbed out."""
    module = importlib.import_module("pipeline_v4")

    posts = [
        {"title": f"post {i}", "url": f"https://example.invalid/{i}",
         "source": "test", "source_type": "academic", "num_comments": i, "score": i}
        for i in range(40)
    ]
    monkeypatch.setattr(module, "collect_all_channels", lambda **kw: list(posts))
    monkeypatch.setattr(module, "filter_by_discussion_quality", lambda items, **kw: list(items))
    monkeypatch.setattr(module, "load_processed_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(module, "filter_already_processed", lambda items, *a, **kw: list(items))
    monkeypatch.setattr(module, "llm_insight_filter", lambda items, **kw: list(items))

    judged = {}

    def fake_judgment(candidates, top_k=10):
        judged["count"] = len(candidates)
        return []

    monkeypatch.setattr(module, "final_pro_judgment", fake_judgment)
    monkeypatch.setattr(module.Path, "mkdir", lambda *a, **kw: None)
    monkeypatch.setattr(module.json, "dump", lambda *a, **kw: None)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: __import__("io").StringIO())
    return module, judged


def test_top_k_defaults_to_no_cap(pipeline) -> None:
    """The default must keep judging everything, as it did before the fix."""
    module, judged = pipeline
    module.run_pipeline_v4()
    assert judged["count"] == 40


def test_top_k_caps_when_given(pipeline) -> None:
    """The regression: max() meant the cap never applied, whatever was passed."""
    module, judged = pipeline
    module.run_pipeline_v4(top_k=5)
    assert judged["count"] == 5


def test_top_k_above_the_pool_is_harmless(pipeline) -> None:
    module, judged = pipeline
    module.run_pipeline_v4(top_k=500)
    assert judged["count"] == 40


def test_forge_budget_comes_from_the_provider_config() -> None:
    forge = importlib.import_module("idea_forge.forge")
    assert forge.IDEATOR_REQUEST["max_tokens"] >= 8192


def test_forge_call_sites_do_not_override_the_configured_budget() -> None:
    source = (REPO_ROOT / "src" / "idea_forge" / "forge.py").read_text(encoding="utf-8")
    for stale in ("max_tokens=700", "max_tokens=1200", "max_tokens=2500", "max_tokens=3000"):
        assert stale not in source
    assert source.count("IDEATOR_REQUEST") >= 3


def test_forge_parallel_runner_caps_workers_and_preserves_order(monkeypatch) -> None:
    forge = importlib.import_module("idea_forge.forge")
    monkeypatch.setattr(forge.llm_client, "configured_max_concurrency", lambda: 3)
    lock = threading.Lock()
    active = 0
    peak = 0

    def work(value):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return value * 2

    assert forge._run_parallel(work, range(12)) == [value * 2 for value in range(12)]
    assert peak == 3


def test_all_expensive_forge_stages_use_the_bounded_runner() -> None:
    source = (REPO_ROOT / "src" / "idea_forge" / "forge.py").read_text(encoding="utf-8")
    assert source.count("_run_parallel(") >= 4


def test_parallel_limit_is_reloaded_for_each_batch(monkeypatch) -> None:
    forge = importlib.import_module("idea_forge.forge")
    limits = iter([1, 3])
    monkeypatch.setattr(forge.llm_client, "configured_max_concurrency", lambda: next(limits))

    assert forge._run_parallel(lambda value: value, [1, 2]) == [1, 2]
    assert forge._run_parallel(lambda value: value, [3, 4]) == [3, 4]


def test_forge_resumes_after_the_last_completed_seed(monkeypatch, tmp_path) -> None:
    forge = importlib.import_module("idea_forge.forge")
    seeds = [
        {"title": "first", "url": "https://example.invalid/first"},
        {"title": "second", "url": "https://example.invalid/second"},
    ]
    checkpoint = tmp_path / "forge.json"
    monkeypatch.setattr(forge, "resolve_directions", lambda _ids=None: [{"id": "b"}])
    monkeypatch.setattr(forge.llm_client, "configured_max_concurrency", lambda: 3)

    calls = []

    def crash_on_second(seed, _directions):
        calls.append(seed["title"])
        if seed["title"] == "second":
            raise RuntimeError("interrupted")
        return []

    monkeypatch.setattr(forge, "step1_deep_ideation", crash_on_second)
    with pytest.raises(RuntimeError, match="interrupted"):
        forge.run_idea_forge(seeds, checkpoint_path=checkpoint)

    saved = __import__("json").loads(checkpoint.read_text(encoding="utf-8"))
    assert [record["seed_title"] for record in saved["results"]] == ["first"]
    assert not checkpoint.with_suffix(".json.tmp").exists()

    calls.clear()
    monkeypatch.setattr(
        forge, "step1_deep_ideation",
        lambda seed, _directions: calls.append(seed["title"]) or [])
    result = forge.run_idea_forge(seeds, checkpoint_path=checkpoint)

    assert calls == ["second"]
    assert result["summary"]["seeds_processed"] == 2


def test_three_month_mode_keeps_a_ceiling(monkeypatch) -> None:
    """The caller is where this last went wrong, and nothing covered the caller.

    run_3month_mode() passed top_k=500 with a comment saying it amounted to no
    limit -- true only while top_k was max(top_k, len(candidates)) and did
    nothing. Once it truncated, the first fix swung to None, which removed the
    ceiling on a 12-channel 90-day window entirely: cost, wall-clock and rate
    limits all unbounded. 355 is the largest run on record, so the cap has
    headroom and still bounds a bad day.
    """
    sys.path.insert(0, str(REPO_ROOT))
    daily = importlib.import_module("idea_generation")

    seen = {}

    def fake_pipeline(**kwargs):
        seen.update(kwargs)
        return {"final_candidates": []}

    monkeypatch.setitem(sys.modules, "pipeline_v4", type(sys)("pipeline_v4"))
    sys.modules["pipeline_v4"].run_pipeline_v4 = fake_pipeline
    forge = type(sys)("idea_forge.forge")
    forge.run_idea_forge = lambda *a, **kw: {"summary": {}}
    b_library = type(sys)("idea_forge.b_library")
    b_library.get_b_library = lambda: []
    b_library.select_b_directions = lambda *a, **k: ([], "stub")
    b_library.print_selection = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "idea_forge.forge", forge)
    monkeypatch.setitem(sys.modules, "idea_forge.b_library", b_library)
    monkeypatch.setattr(daily, "log", lambda *a, **kw: None)

    daily.run_3month_mode()

    assert seen.get("top_k") == daily.THREE_MONTH_TOP_K
    assert isinstance(daily.THREE_MONTH_TOP_K, int) and daily.THREE_MONTH_TOP_K > 0, (
        "an unbounded default turns a wide collection window into an unbounded bill"
    )


def test_three_month_ceiling_is_overridable(monkeypatch) -> None:
    monkeypatch.setenv("AR_3MONTH_TOP_K", "1200")
    sys.path.insert(0, str(REPO_ROOT))
    daily = importlib.reload(importlib.import_module("idea_generation"))
    assert daily.THREE_MONTH_TOP_K == 1200


@pytest.mark.parametrize("bad", [-1, -100])
def test_a_negative_cap_is_rejected(pipeline, bad: int) -> None:
    """ranked[:-1] drops one candidate instead of bounding them.

    AR_3MONTH_TOP_K=-1 and --top-k -1 both land here, and the truncation line
    would then report a count that cannot be true. Validating at the shared
    consumer covers the environment variable and the CLI at once.
    """
    module, _ = pipeline
    with pytest.raises(ValueError, match="top_k must be >= 0"):
        module.run_pipeline_v4(top_k=bad)


def test_zero_means_no_pro_judgment(pipeline) -> None:
    """Zero is a legitimate budget, not an error."""
    module, judged = pipeline
    module.run_pipeline_v4(top_k=0)
    assert judged["count"] == 0
