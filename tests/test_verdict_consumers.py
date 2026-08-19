"""判定只解析一次，渲染和筛选都读那一次的结果。

单测覆盖解析器本身在 test_verdicts.py。这里驱动真实的渲染和筛选路径，因为「解析器对了」
和「消费者用了它」是两件事：判定词的子串测试原先散在 idea_generation、generate_dashboard、
generate_idea_page 三个文件的十几处，每处各判一遍，改一处不影响另外两处。

用例都用历史语料里真实出现过的形状，不是构造的。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

import generate_dashboard as dashboard  # noqa: E402
import generate_idea_page as page  # noqa: E402
from verdicts import STRONG, UNPARSED  # noqa: E402

# 判定是【值得深入】，「不适合」在理由里。历史语料里 8 条长这样。
# ai-tell-scan: ignore  以下三个常量是语料原文
WORTH_WITH_NEGATION_IN_REASON = (
    "**【值得深入】——它提供了一个极佳的批判性理论视角，"
    "但不适合作为即插即用的工程提点模块（A种子）。"
)
NEGATED = "最终判定: 不值得深入"
CONDITIONAL = "**【不适合做A种子】（针对纯算法团队） / 【强推荐】（针对SysML/大厂推理团队）**"


def seed(conclusion, **extra):
    record = {"title": "t", "url": "u", "conclusion": conclusion}
    record.update(extra)
    return record


def tag_of(html):
    """渲染出来的判定标签，取 (class 后缀, 文字)。

    断言 class 而不是文字：两个渲染器的文案不同（「不适合做 A 种子」和「不适合」），
    class 是共用的。也不按位置切 HTML：第一版用 `split("</span>")[0]`，对其中一个
    渲染器取到了错的片段，于是那一半在旧代码上也是绿的。
    """
    found = re.search(r'tag-(\w+)"?>([^<]*)', html)
    assert found, "没有渲染出判定标签"
    return found.group(1), found.group(2)


@pytest.mark.parametrize("render", [dashboard.render_seed_card, page.render_seed_card])
def test_a_negation_in_the_reason_does_not_change_the_rendered_tag(render):
    assert tag_of(render(1, seed(WORTH_WITH_NEGATION_IN_REASON)))[0] == "worth"


@pytest.mark.parametrize("render", [dashboard.render_seed_card, page.render_seed_card])
def test_a_negated_verdict_is_not_rendered_as_the_positive_one(render):
    """`"值得" in conclusion` 把「不值得深入」标成了值得深入。"""
    assert tag_of(render(1, seed(NEGATED)))[0] == "skip"


@pytest.mark.parametrize("render", [dashboard.render_seed_card, page.render_seed_card])
def test_an_unreadable_verdict_says_so_rather_than_a_judgement(render):
    """读不出和 judge 判了「不适合」原先都落到同一个标签，事后分不出。"""
    assert "读不出" in tag_of(render(1, seed(CONDITIONAL)))[1]


@pytest.mark.parametrize("render", [dashboard.render_seed_card, page.render_seed_card])
def test_the_stored_verdict_wins_over_the_text(render):
    """新数据带 conclusion_verdict，渲染不该再去猜那句中文。"""
    assert tag_of(render(1, seed("随便一句没有判定词的话", conclusion_verdict=STRONG)))[0] == "strong"


def test_only_strong_and_worth_seeds_reach_the_forge(monkeypatch):
    """跑真实的 run_3month_mode，看它把哪些种子交给 Forge。

    「不值得深入」和条件性双判定曾经都落进 worth：前者因为 `"值得" in conclusion`，
    后者因为它含「强推荐」。每颗种子进 Forge 要花掉一整轮构思和交叉评审。
    """
    import types

    import idea_generation

    # ai-tell-scan: ignore  语料原文
    candidates = [
        seed("最终判定: 强推荐 —— 好"),
        seed(WORTH_WITH_NEGATION_IN_REASON),
        seed(NEGATED),
        seed(CONDITIONAL),
        seed("最终判定: 不适合做A种子 —— 是观点文章"),
    ]

    pipeline = types.ModuleType("pipeline_v4")
    pipeline.run_pipeline_v4 = lambda **kw: {"final_candidates": candidates}
    monkeypatch.setitem(sys.modules, "pipeline_v4", pipeline)

    sent = {}
    forge = types.ModuleType("idea_forge.forge")
    forge.run_idea_forge = lambda seeds, b_ids=None: sent.update(seeds=seeds) or {"summary": {}}
    monkeypatch.setitem(sys.modules, "idea_forge.forge", forge)

    lib = types.ModuleType("idea_forge.b_library")
    lib.select_b_directions = lambda *a, **k: ([{"id": "b"}], "stub")
    lib.print_selection = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "idea_forge.b_library", lib)
    monkeypatch.setattr(idea_generation, "log", lambda *a, **k: None)

    idea_generation.run_3month_mode()

    forged = [c["conclusion"] for c in sent["seeds"]]
    assert WORTH_WITH_NEGATION_IN_REASON in forged, "判定是值得深入，理由里的否定不算数"
    assert NEGATED not in forged
    assert CONDITIONAL not in forged
    assert len(forged) == 2


def test_the_pipeline_records_the_verdict_next_to_the_line(monkeypatch):
    """产出里既要有给人看的原文，也要有给下游读的档位。"""
    import pipeline_v4

    monkeypatch.setattr(pipeline_v4, "call_role",
        lambda *a, **k: "核心insight: x\n**研判结论:** \n【强推荐做A种子】",
    )
    judged = pipeline_v4.final_pro_judgment([{"title": "t", "url": "u"}], top_k=1)

    assert judged[0]["conclusion_verdict"] == STRONG
    assert judged[0]["conclusion"] == "【强推荐做A种子】"


def test_an_unreadable_judgment_is_recorded_with_its_reason(monkeypatch, capsys):
    """读不出和「不适合」在下游一样不进 Forge，产出里必须能分开。"""
    import pipeline_v4

    monkeypatch.setattr(pipeline_v4, "call_role", lambda *a, **k: "写了一堆但没给判定")
    judged = pipeline_v4.final_pro_judgment([{"title": "t", "url": "u"}], top_k=1)

    assert judged[0]["conclusion_verdict"] == UNPARSED
    assert judged[0]["conclusion_unparsed_reason"]
    assert "读不出判定" in capsys.readouterr().out


def test_the_two_pages_agree_on_the_same_record(tmp_path, monkeypatch):
    """ideas.html 只从候选里挑几个字段建卡片，`conclusion_verdict` 曾经不在其中。

    dashboard 保留整条记录并读那个字段，ideas 页则退回去解析 conclusion 原文，于是
    同一条新数据在两个页面上可以显示不同的判定。
    """
    import json

    verified = tmp_path / "verified"
    verified.mkdir()
    (verified / "pipeline_v4_20260810.json").write_text(
        json.dumps({"final_candidates": [{
            "title": "t", "url": "u",
            "conclusion": "一句没有判定词的话",
            "conclusion_verdict": STRONG,
        }]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(page, "VERIFIED_DIR", verified)
    monkeypatch.setattr(page, "FORGE_DIR", tmp_path / "empty")

    timeline = page.collect_by_date()
    card = timeline[0]["seeds"][0]

    assert tag_of(page.render_seed_card(1, card))[0] == "strong"
    assert tag_of(dashboard.render_seed_card(1, card))[0] == "strong"
