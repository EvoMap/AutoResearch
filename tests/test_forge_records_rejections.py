"""一颗种子被淘汰在哪一关，产出里要看得出来。

2026-08-10 的第一次真实跑批：一颗种子产出 3 个 idea、跑了 9 次交叉评审、全部被否，而
落盘的 summary 是 `total_ideas: 0, results: []`。因为只有通过验证的种子才会被写进
`all_results`，被淘汰的连同评审原文一起丢掉。

后果有两个，都指向同一件事：从产出里分不清「没产生 idea」和「产生了 3 个但都被否」。
前者说明构思那步坏了，后者说明它工作正常而这颗种子不行——要做的事完全不同。而评审原文
不留，事后也没法复核那 9 次否决是不是判对了，尤其在判定解析刚改过的时候。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import llm_client_double

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


@pytest.fixture
def forge(monkeypatch, tmp_path):
    """真实的 run_idea_forge，模型调用换成可控的桩，落盘换到临时目录。"""
    stub = llm_client_double(call_pro=lambda *a, **k: "",
                             call_role=lambda role, prompt, **k: "")
    monkeypatch.setitem(sys.modules, "llm_client", stub)

    import importlib

    import idea_forge.forge as module
    importlib.reload(module)
    monkeypatch.setattr(module, "step2_5_freshness_refresh", lambda items, **k: items)
    monkeypatch.setattr(module, "filter_by_consensus", lambda items: items)
    monkeypatch.setattr(module, "step3_plan_generation", lambda items: items)
    monkeypatch.setattr(module, "resolve_directions", lambda b_ids=None: [
        {"id": "b", "domain": "d", "problem": "p", "knowledge_md": "", "datasets": [], "baselines": []}])
    monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path, raising=False)
    return module


def run(module, ideas, validated, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "step1_deep_ideation", lambda seed, lib: ideas)
    monkeypatch.setattr(module, "step2_strict_validation", lambda items: validated)
    checkpoint = tmp_path / f"forge-{len(list(tmp_path.glob('forge-*.json')))}.json"
    module.run_idea_forge([{"title": "seed"}], checkpoint_path=checkpoint)
    return json.loads(checkpoint.read_text(encoding="utf-8"))


def test_ideas_that_were_generated_are_counted_even_when_all_are_rejected(forge, tmp_path, monkeypatch):
    """`total_ideas: 0` 会让人以为构思那步坏了，而它其实产出了 3 个。"""
    ideas = [{"idea_text": f"idea {n}", "b_id": "b"} for n in range(3)]

    written = run(forge, ideas, [], tmp_path, monkeypatch)

    assert written["summary"]["total_ideas"] == 3
    assert written["summary"]["total_validated"] == 0


def test_the_stage_a_seed_died_at_is_recorded(forge, tmp_path, monkeypatch):
    """「一个 idea 都没产生」和「产生了但都被否」要分得开。"""
    written = run(forge, [], [], tmp_path, monkeypatch)
    assert written["results"][0]["stopped_at"] == "ideation"

    ideas = [{"idea_text": "x", "b_id": "b"}]
    written = run(forge, ideas, [], tmp_path, monkeypatch)
    assert written["results"][0]["stopped_at"] == "validation"


def test_rejected_ideas_are_saved_in_full(forge, tmp_path, monkeypatch):
    idea_text = "机制说明" * 300 + "完整实验设计"
    ideas = [{"idea_text": idea_text, "b_id": "b"}]

    written = run(forge, ideas, [], tmp_path, monkeypatch)

    assert written["results"][0]["rejected"][0]["idea_text"] == idea_text


def test_reviewers_receive_the_full_idea_and_full_reviews_are_saved(forge, monkeypatch):
    idea_text = "前文" * 400 + "关键实验尾部"
    freshness_warning = "D4：" + "时新性证据" * 30 + "关键时新性尾部"
    review_text = (
        "D1通过：机制成立\n"
        "D2通过：方法简洁\n"
        "D3不通过：" + "实验理由" * 100 + "\n"
        + freshness_warning + "\n"
        "verdict: 不通过"
    )
    prompts = []

    def review(model, prompt, **kwargs):
        prompts.append(prompt)
        return review_text

    monkeypatch.setattr(forge, "IDEA_MODELS", ["stub-a", "stub-b", "stub-c"])
    monkeypatch.setattr(forge, "call_idea_model", review)
    item = {"idea_text": idea_text, "source_model": "stub-a"}

    forge.step2_strict_validation([item])

    assert idea_text in prompts[0]
    assert item["validation"]["reviews"][0]["review"] == review_text
    assert "关键时新性尾部" in item["freshness_flags"][0]


def test_core_summary_keeps_the_complete_core_line(forge, monkeypatch):
    core_line = "核心idea（一句话）: " + "机制" * 100 + "核心句尾部"
    monkeypatch.setattr(forge, "IDEA_MODELS", ["stub-a", "stub-b", "stub-c"])
    monkeypatch.setattr(forge, "call_idea_model", lambda *a, **k: core_line + "\n关键实验: 完整")

    ideas = forge.step1_deep_ideation(
        {"title": "seed"},
        [{"id": "b", "domain": "d", "problem": "p"}],
    )

    assert ideas[0]["core_summary"] == core_line


def test_the_plan_count_leaves_out_every_failure_marker(forge, tmp_path, monkeypatch):
    """记数认得的失败标记比页面少一个时，失败会被记成交付（#211）。

    `计划书生成失败` 是早先版本写的标记，`data/idea_forge/forge_20260509_1922.json` 的
    4 条记录全是它：产出里记着交付了 4 份计划书，页面一份都不渲染。
    """
    ideas = [{"idea_text": "x", "b_id": "b", "plan": "一份真的计划书"},
             {"idea_text": "y", "b_id": "b", "plan": "计划书生成失败"},
             {"idea_text": "z", "b_id": "b", "plan": "生成失败"}]

    written = run(forge, ideas, ideas, tmp_path, monkeypatch)

    assert written["results"][0]["plans"] == 1


def test_a_seed_that_passes_is_not_marked_as_stopped(forge, tmp_path, monkeypatch):
    ideas = [{"idea_text": "x", "b_id": "b"}]

    written = run(forge, ideas, ideas, tmp_path, monkeypatch)

    assert written["results"][0].get("stopped_at") is None
    assert written["summary"]["total_validated"] == 1
