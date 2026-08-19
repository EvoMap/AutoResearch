"""Tests for how src/generate_idea_page.py reads forged plans.

run_idea_forge() writes the plan objects under "results" and stores only a count
under "plans" (forge.run_idea_forge() 的 summary-378). Reading "plans" as a list meant no forged plan ever
reached the page, and raised TypeError once the count was non-zero. The exception was
swallowed by idea_generation.py, so the visible symptom was a page that never updated.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import generate_idea_page as page  # noqa: E402


def plan_entry(**overrides):
    entry = {
        "b_domain": "agent_memory",
        "b_problem": "long-horizon recall",
        "source_model": "model-a",
        "idea_text": "an idea",
        "plan": "a plan",
        "validation": {"freshness_flags": []},
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def forge_dir(tmp_path, monkeypatch):
    """Point the module at an empty pair of data directories."""
    forge = tmp_path / "idea_forge"
    verified = tmp_path / "verified"
    forge.mkdir()
    verified.mkdir()
    monkeypatch.setattr(page, "FORGE_DIR", forge)
    monkeypatch.setattr(page, "VERIFIED_DIR", verified)
    return forge


def write_forge(forge_dir: Path, payload: dict, name: str = "forge_20260803_1200.json") -> None:
    (forge_dir / name).write_text(json.dumps(payload, ensure_ascii=False))


def collected_plans(timeline) -> list:
    return [plan for entry in timeline for plan in entry["plans"]]


def test_reads_plans_from_the_results_field(forge_dir):
    """The shape run_idea_forge actually writes."""
    write_forge(forge_dir, {
        "results": [{
            "seed_title": "a seed",
            "total_ideas": 6,
            "validated": 1,
            "plans": 1,               # a count, not a list
            "results": [plan_entry()],  # the plan objects live here
        }]
    })

    plans = collected_plans(page.collect_by_date())

    assert len(plans) == 1
    assert plans[0]["seed_title"] == "a seed"
    assert plans[0]["b_domain"] == "agent_memory"
    assert plans[0]["plan"] == "a plan"


def test_integer_plans_count_does_not_raise(forge_dir):
    """The regression: iterating the count raised TypeError.

    data/idea_forge/forge_20260528_1729.json ships results[0].plans = 2.
    """
    write_forge(forge_dir, {
        "results": [{"seed_title": "s", "plans": 2, "results": [plan_entry(), plan_entry()]}]
    })

    timeline = page.collect_by_date()  # must not raise

    assert len(collected_plans(timeline)) == 2


def test_falls_back_to_a_list_under_plans(forge_dir):
    """Any output that does carry the list under "plans" must still render."""
    write_forge(forge_dir, {"results": [{"seed_title": "s", "plans": [plan_entry()]}]})

    plans = collected_plans(page.collect_by_date())

    assert len(plans) == 1
    assert plans[0]["b_domain"] == "agent_memory"


def test_entry_with_neither_shape_is_skipped(forge_dir):
    write_forge(forge_dir, {"results": [{"seed_title": "s", "plans": 0}]})

    assert collected_plans(page.collect_by_date()) == []


def test_malformed_entries_do_not_take_down_the_page(forge_dir):
    """One bad record must not cost the whole page, which is what the try in
    idea_generation.py used to hide."""
    write_forge(forge_dir, {
        "results": [
            "not a dict",
            {"seed_title": "good", "results": [plan_entry(), "not a plan", 42]},
        ]
    })

    plans = collected_plans(page.collect_by_date())

    assert len(plans) == 1
    assert plans[0]["seed_title"] == "good"


def test_plans_are_grouped_under_the_file_date(forge_dir):
    write_forge(forge_dir, {"results": [{"seed_title": "s", "results": [plan_entry()]}]},
                name="forge_20260803_1200.json")
    write_forge(forge_dir, {"results": [{"seed_title": "t", "results": [plan_entry()]}]},
                name="forge_20260509_0900.json")

    timeline = page.collect_by_date()

    dates = [entry["date"] for entry in timeline]
    assert dates == sorted(dates, reverse=True), "timeline is newest first"
    assert "20260803" in dates and "20260509" in dates


def test_unreadable_file_is_skipped(forge_dir):
    (forge_dir / "forge_20260803_1200.json").write_text("{ not json")

    assert page.collect_by_date() == [] or collected_plans(page.collect_by_date()) == []


def test_failed_plan_records_are_not_rendered(forge_dir):
    """"results" carries every record, including failures.

    Only the "plans" *count* excludes them (forge.run_idea_forge() 的 Step 3). Rendering the list as-is
    presented 生成失败 to the reader as a delivered plan; against the repository's own
    data that was 28 of 106 cards.
    """
    write_forge(forge_dir, {
        "results": [{
            "seed_title": "s",
            "plans": 1,
            "results": [
                plan_entry(plan="a real plan"),
                plan_entry(plan="生成失败"),
                plan_entry(plan="计划书生成失败"),
                plan_entry(plan=""),
            ],
        }]
    })

    plans = collected_plans(page.collect_by_date())

    assert len(plans) == 1
    assert plans[0]["plan"] == "a real plan"


def test_rendered_card_shows_the_real_vote_counts(forge_dir):
    """step2_strict_validation writes votes_pass/total_reviews.

    Reading only the older votes/total keys rendered every card as (0/0).
    """
    html = page.render_plan_card(1, plan_entry(
        validation={"votes_pass": 2, "total_reviews": 3, "freshness_flags": []},
    ))

    assert "(2/3)" in html
    assert "(0/0)" not in html


def test_rendered_card_falls_back_to_legacy_vote_keys(forge_dir):
    html = page.render_plan_card(1, plan_entry(
        validation={"votes": 1, "total": 2, "freshness_flags": []},
    ))

    assert "(1/2)" in html


def test_rendered_card_omits_counts_when_unknown(forge_dir):
    """Never print (0/0); say it passed without inventing a tally."""
    html = page.render_plan_card(1, plan_entry(validation={"freshness_flags": []}))

    assert "严格交叉验证通过" in html
    assert "(0/0)" not in html


def test_has_usable_plan_matches_the_producer_definition():
    assert page.has_usable_plan({"plan": "something"})
    assert not page.has_usable_plan({"plan": "生成失败"})
    assert not page.has_usable_plan({"plan": "计划书生成失败"})
    assert not page.has_usable_plan({"plan": "   "})
    assert not page.has_usable_plan({})


def test_plan_mentioning_failure_in_its_text_is_kept(forge_dir):
    """Equality, not substring.

    The producer compares the whole value (forge.run_idea_forge() 的 Step 3). Matching a substring would
    delete a real plan whose body happens to discuss what to do if generation fails.
    """
    write_forge(forge_dir, {
        "results": [{
            "seed_title": "s",
            "results": [
                plan_entry(plan="第一步：跑基线。若生成失败，重试一次，然后继续实验。"),
                plan_entry(plan="生成失败"),
            ],
        }]
    })

    plans = collected_plans(page.collect_by_date())

    assert len(plans) == 1
    assert plans[0]["plan"].startswith("第一步")


def test_has_usable_plan_compares_whole_values(forge_dir):
    assert page.has_usable_plan({"plan": "若生成失败则重试"})
    assert page.has_usable_plan({"plan": "  a real plan  "})
    assert not page.has_usable_plan({"plan": "生成失败"})
    assert not page.has_usable_plan({"plan": "  生成失败  "})
    assert not page.has_usable_plan({"plan": ""})
