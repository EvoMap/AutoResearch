"""构思席位是配置里那一栏，不是模块里写死的三个名字。

preflight 探的是 `roles.ideator.models`（五个候选、至少三个独立模型），而 forge 把推荐值
写在调用参数里，配置那一栏运行时一次都读不到：检查器报的是一组模型，跑起来用的是另一组。
`freshness` 那处更彻底——它先取角色的第一个候选当模型名调，失败了写死回落 `gpt-5.5`，于是
配置里那条候选链一个都到不了，而 preflight 逐个探的正是它（#190）。

所以这里钉四件事：席位名单来自解析器、计划书的模型同样来自它、一个席位发不出去只损失
它自己那一票、以及名字写错仍然立刻抛——那是配置的问题，换个席位救不了。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import llm_client  # noqa: E402


@pytest.fixture
def forge_with(monkeypatch):
    """按给定的「配置候选」重新加载 forge，用完把模块恢复成读真配置的那版。

    席位名单是模块常量，所以判据只能在 import 时看。恢复这一步不能省：模块对象是
    进程级的，留着假名单会让后面任何 import forge 的用例跟着一起假。
    """
    import idea_forge.forge as forge

    def load(**candidates):
        monkeypatch.setattr(llm_client, "_configured_candidates",
                            lambda role, config=None: candidates.get(role, []))
        return importlib.reload(forge)

    yield load
    monkeypatch.undo()
    importlib.reload(forge)


# ---- 席位从配置来 ----

def test_the_seats_are_the_configured_candidates(forge_with):
    """配置里写几个席位就是几个，不是代码里那三个。"""
    forge = forge_with(ideator=["cfg-a", "cfg-b", "cfg-c", "cfg-d"])

    assert forge.IDEA_MODELS == ["cfg-a", "cfg-b", "cfg-c", "cfg-d"]


def test_the_plan_model_is_the_first_configured_candidate(forge_with):
    """计划书要的是一份长文，不是多份投票，所以取第一个候选。"""
    forge = forge_with(planner=["cfg-planner", "cfg-backup"])

    assert forge.PLAN_MODEL == "cfg-planner"


def test_the_override_still_wins_over_the_configured_seats(forge_with, monkeypatch):
    """`AR_MODEL_IDEATOR` 是第一顺位，配置是第二顺位。两者都在时听前者。"""
    monkeypatch.setenv("AR_MODEL_IDEATOR", "mine-1,mine-2,mine-3")
    forge = forge_with(ideator=["cfg-a", "cfg-b", "cfg-c"])

    assert forge.IDEA_MODELS == ["mine-1", "mine-2", "mine-3"]


def test_the_seats_fall_back_to_the_backstop_table(forge_with):
    """别人拿走 src/ 单独用、读不到配置时，面板仍然要是多个模型。

    默认仍保留多个模型，以获得真正独立的交叉评审。
    """
    forge = forge_with()  # 配置里什么都没有

    assert forge.IDEA_MODELS == llm_client.ROLE_DEFAULTS["ideator"]
    assert len(set(forge.IDEA_MODELS)) >= 3


def test_the_banner_counts_the_seats_it_actually_has(forge_with, monkeypatch, capsys):
    """标题行写死「× 3 模型」的话，换了席位数之后它就在说谎。"""
    forge = forge_with(ideator=["cfg-a", "cfg-b", "cfg-c", "cfg-d"])
    monkeypatch.setattr(forge, "call_idea_model", lambda *a, **k: None)

    forge.step1_deep_ideation({"title": "seed"}, [{"id": "b", "domain": "d", "problem": "p"}])

    assert "× 4 模型" in capsys.readouterr().out


def test_a_short_panel_is_rejected_before_review(
        forge_with, monkeypatch):
    forge = forge_with(ideator=["only-one"])
    monkeypatch.setattr(
        forge, "call_idea_model",
        lambda *a, **k: "D1通过\nD2通过\nD3通过\nD4时新性问题：无\nverdict: 通过",
    )

    with pytest.raises(RuntimeError, match="至少 3 个不同模型"):
        forge.step2_strict_validation([{"idea_text": "idea", "source_model": "only-one"}])


# ---- 一个席位发不出去，只损失一票 ----

def test_a_seat_that_cannot_be_reached_costs_one_seat(forge_with, monkeypatch):
    """缺凭据时 `call_model` 抛的是 RuntimeError，整轮构思会停在第一个配歪的端点上。"""
    forge = forge_with(ideator=["a", "b"])

    def boom(model, prompt, **kw):
        raise RuntimeError("no credential for this endpoint")

    monkeypatch.setattr(forge, "call_model", boom)

    assert forge.call_idea_model("a", "hi") is None


def test_the_round_keeps_the_other_seats(forge_with, monkeypatch):
    """判据是这一轮的产出，不是某个函数吞没吞异常。"""
    forge = forge_with(ideator=["down", "up-1", "up-2"])

    def dispatch(model, prompt, **kw):
        if model == "down":
            raise RuntimeError("endpoint down")
        return "核心idea: 有点意思"

    monkeypatch.setattr(forge, "call_model", dispatch)

    ideas = forge.step1_deep_ideation(
        {"title": "seed"}, [{"id": "b", "domain": "d", "problem": "p"}])

    assert [idea["source_model"] for idea in ideas] == ["up-1", "up-2"]


def test_a_name_the_runtime_does_not_know_still_raises(forge_with, monkeypatch):
    """名字不认识是配置写错，换个席位也救不了——跟 `call_role` 的分法一致。"""
    forge = forge_with(ideator=["typo"])

    def unknown(model, prompt, **kw):
        raise llm_client.UnknownModel(f"没有 {model}")

    monkeypatch.setattr(forge, "call_model", unknown)

    with pytest.raises(llm_client.UnknownModel):
        forge.call_idea_model("typo", "hi")


# ---- 时新性刷新走同一条路 ----

STALE_IDEA = {
    "idea_text": "用 LLaVA-1.5 当主基线，在 Vicuna 上验证。" + "补充说明。" * 40,
    "b_id": "b",
}


@pytest.fixture
def freshness(monkeypatch):
    """刷新那一步，外部依赖（B 库、arxiv）挡掉，只留模型调用。

    先重新加载一次：别的用例往 `sys.modules` 塞过 llm_client 的替身，而这个模块在 import
    时就把 `call_model` / `call_role_with_model` 绑死了。谁先 import 决定它绑到哪一份，
    于是单跑这个文件是绿的、整套跑是红的——判据变成了「替身返回了什么」。
    """
    import idea_forge.freshness as module

    assert sys.modules["llm_client"] is llm_client, "sys.modules 里还留着 llm_client 的替身"
    module = importlib.reload(module)
    monkeypatch.setattr(module, "build_b_library_reference", lambda b_id: "")
    return module


def test_the_refresher_walks_the_configured_chain(freshness, monkeypatch):
    """原来这里只取第一个候选，失败了写死回落 `gpt-5.5`：配置那条链一个都到不了。"""
    tried = []

    def dispatch(model, prompt, **kw):
        tried.append(model)
        return None if model == "首选" else "升级后的方案。" * 40

    monkeypatch.setattr(llm_client, "_configured_candidates",
                        lambda role, config=None: ["首选", "备选"]
                        if role == "freshness_refresher" else [])
    monkeypatch.setattr(llm_client, "call_model", dispatch)

    item = dict(STALE_IDEA)
    assert freshness.refresh_idea_freshness(item, enable_arxiv=False) is True
    assert tried == ["首选", "备选"]


def test_the_refresher_recorded_is_the_one_that_answered(freshness, monkeypatch):
    """回落发生时首选和实际发出去的不是同一个。落盘写首选，就是写了一条假的出处。"""
    monkeypatch.setattr(llm_client, "_configured_candidates",
                        lambda role, config=None: ["首选", "备选"]
                        if role == "freshness_refresher" else [])
    monkeypatch.setattr(llm_client, "call_model",
                        lambda model, prompt, **kw: None if model == "首选"
                        else "升级后的方案。" * 40)

    item = dict(STALE_IDEA)
    freshness.refresh_idea_freshness(item, enable_arxiv=False)

    assert item["freshness_refresh"]["refresher"] == "备选"


def test_naming_a_model_explicitly_still_bypasses_the_role(freshness, monkeypatch):
    """`refresher_model=` 是「就用这个」，不该被角色的候选链覆盖掉。"""
    seen = []
    # 点名那条分支用的是 freshness 自己 import 进来的 call_model，不是角色链里那个。
    monkeypatch.setattr(freshness, "call_model",
                        lambda model, prompt, **kw: seen.append(model) or "升级后的方案。" * 40)

    item = dict(STALE_IDEA)
    freshness.refresh_idea_freshness(item, enable_arxiv=False, refresher_model="点名的")

    assert seen == ["点名的"]
    assert item["freshness_refresh"]["refresher"] == "点名的"
