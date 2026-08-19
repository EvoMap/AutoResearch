"""每个角色有推荐模型，换成别的也能跑，而且换一个不影响别人。

用户的原话：端点和 provider 不重要，重要的是每个 role 有推荐的模型、别的模型也行。

在这之前，「别的也行」这句话在实现里是不成立的：

  judge / planner / freshness_refresher / ideator 共用一个 OPENAI_MID_MODEL，
  改一个动四个；code_reviewer 和 critic 共用 GEMINI_REVIEW_MODEL；agent 的旋钮
  叫 ANTHROPIC_MODEL，名字和角色毫无关系；大多数候选压根没有旋钮，要换得编辑 JSON。

所以这里钉的是三件事：一个角色一个旋钮、换任意模型名都认、以及 preflight 和运行时读的
是同一个覆盖（只让检查器认，就会报告说用 A 实际跑 B）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import roles  # noqa: E402


def test_a_role_uses_its_recommended_model_by_default():
    assert roles.models_for("judge", "gemini-3.1-pro", env={}) == ["gemini-3.1-pro"]


def test_any_model_name_can_replace_it():
    """推荐不是要求。填什么名字都认，我们不维护一份「允许的模型」白名单。"""
    env = {"AR_MODEL_JUDGE": "some-model-we-never-heard-of"}

    assert roles.models_for("judge", "gemini-3.1-pro", env=env) == \
        ["some-model-we-never-heard-of"]


def test_changing_one_role_does_not_move_the_others():
    """原来 judge / planner / freshness_refresher / ideator 共用 OPENAI_MID_MODEL。"""
    env = {"AR_MODEL_JUDGE": "mine"}

    assert roles.models_for("judge", "rec", env=env) == ["mine"]
    assert roles.models_for("planner", "rec", env=env) == ["rec"]
    assert roles.models_for("freshness_refresher", "rec", env=env) == ["rec"]


def test_the_panel_role_takes_several_models():
    """Idea Forge 的三席面板一个模型只占一个席位。"""
    env = {"AR_MODEL_IDEATOR": "a, b ,c"}

    assert roles.models_for("ideator", ["x", "y", "z"], env=env) == ["a", "b", "c"]


def test_a_single_seat_role_ignores_the_extra_names():
    """写 `AR_MODEL_JUDGE=a,b` 的人想说的是 a。为此让整条管线起不来不值得。"""
    assert roles.models_for("judge", "rec", env={"AR_MODEL_JUDGE": "a,b"}) == ["a"]


@pytest.mark.parametrize("value", ["", "   ", ",,"])
def test_an_empty_override_falls_back_to_the_recommendation(value):
    """`.env` 里留个空值是常事，不该把角色变成没有模型。"""
    assert roles.models_for("judge", "rec", env={"AR_MODEL_JUDGE": value}) == ["rec"]


def test_the_override_replaces_rather_than_appends():
    """「我要用这些」比「在推荐之上再加这些」更常见，也更容易预期。"""
    env = {"AR_MODEL_IDEATOR": "only-mine"}

    assert roles.models_for("ideator", ["a", "b"], env=env) == ["only-mine"]


def test_the_report_line_says_recommended_and_actual():
    """报告要能一眼看出「这是推荐值」还是「我换过了」。"""
    plain = roles.describe("judge", "gemini-3.1-pro", env={})
    assert "推荐 gemini-3.1-pro" in plain and "本次用" not in plain

    changed = roles.describe("judge", "gemini-3.1-pro", env={"AR_MODEL_JUDGE": "mine"})
    assert "推荐 gemini-3.1-pro" in changed
    assert "本次用 mine" in changed
    assert "AR_MODEL_JUDGE" in changed, "要告诉人是哪个变量改的"


def test_the_knob_is_named_after_the_role():
    """原来是 ANTHROPIC_MODEL 控制 agent、GEMINI_REVIEW_MODEL 控制两个角色。"""
    assert roles.env_name("freshness_refresher") == "AR_MODEL_FRESHNESS_REFRESHER"


# ---- preflight 和运行时必须读同一个覆盖 ----

def test_the_forge_seats_follow_the_override(monkeypatch):
    """IDEA_MODELS 和 PLAN_MODEL 原来是模块常量，改模型得改代码。

    这里走真的 llm_client：判据是覆盖穿过解析器到达席位名单，换成桩就只剩「桩返回了什么」。
    """
    monkeypatch.setenv("AR_MODEL_IDEATOR", "m1,m2")
    monkeypatch.setenv("AR_MODEL_PLANNER", "planner-model")
    import importlib

    import idea_forge.forge as forge
    importlib.reload(forge)

    assert forge.IDEA_MODELS == ["m1", "m2"]
    assert forge.PLAN_MODEL == "planner-model"


def test_preflight_reports_the_same_model_the_run_will_use(monkeypatch):
    """只让 preflight 认覆盖，就会「报告说用 A、实际跑 B」。"""
    monkeypatch.setenv("AR_MODEL_JUDGE", "chosen-by-me")
    sys.path.insert(0, str(REPO / "scripts"))
    import importlib

    import preflight
    importlib.reload(preflight)

    profile = {"api": "openai_chat", "model": "recommended-one"}
    assert preflight.model_for_role("judge", profile) == "chosen-by-me"
    assert preflight.model_for_role("planner", profile) == "recommended-one"
