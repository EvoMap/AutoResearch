"""每个角色的模型覆盖都要走到运行时，不能只被检查器认。

`AR_MODEL_SCREENER` 和 `AR_MODEL_JUDGE` 现在的状态：preflight 按它们探、README 写了它们，
而运行时压根不读——`pipeline_v4` 直接调 `call_flash` / `call_pro`，那两个函数把 preset 名
写死在自己里面。于是「换个模型」这句话对六个角色里的两个是假的，而且是绿着假的。

这正是这个仓库反复清理的那个形状：检查器和运行时各读各的。所以判据不是「函数存在」，
是「设了变量之后，真正发出去的那个模型名变了」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "collectors"))

import llm_client  # noqa: E402
import roles  # noqa: E402

# 每个角色一条：调用点在哪、它默认用哪个模型。
@pytest.fixture
def seen(monkeypatch):
    """拦住真正的派发，记下它收到的模型名。"""
    captured: list[str] = []
    monkeypatch.setattr(llm_client, "call_model",
                        lambda name, prompt, **kw: captured.append(name) or "ok")
    return captured


def test_every_documented_knob_belongs_to_a_role():
    """README 和 .env.example 列的那些变量，必须是真角色的旋钮。"""
    documented = {line.split("=")[0].strip()
                  for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
                  if line.strip().startswith("AR_MODEL_")}

    for name in documented:
        role = name.removeprefix("AR_MODEL_").lower()
        assert roles.env_name(role) == name, f"{name} 不对应任何角色"


@pytest.mark.parametrize("role", ["screener", "judge", "consensus_checker",
                                  "freshness_refresher"])
def test_the_override_changes_what_the_runtime_sends(role, seen, monkeypatch):
    """判据是「发出去的模型名变了」，不是「有个函数叫 call_role」。"""
    monkeypatch.setenv(roles.env_name(role), f"model-for-{role}")

    llm_client.call_role(role, "ping")

    assert seen == [f"model-for-{role}"], f"{role} 的覆盖没走到运行时"


def test_without_an_override_the_configured_first_choice_is_used(seen, monkeypatch):
    """真相源是配置里那个角色的候选，不是 ROLE_DEFAULTS。

    那张表写的是单个名字，配置写的是三到五个候选的链。用表当首选的话，preflight
    逐个探过的备选在运行时一个都到不了。
    """
    monkeypatch.delenv(roles.env_name("screener"), raising=False)

    llm_client.call_role("screener", "ping")

    assert seen and seen[0] == llm_client._configured_candidates("screener")[0]


def test_the_hardcoded_default_is_only_a_backstop(monkeypatch):
    """配置读不出来时才用它——别人拿走 src/ 单独用会命中这条路。"""
    monkeypatch.setattr(llm_client, "_configured_candidates", lambda role, config=None: [])
    seen = []
    monkeypatch.setattr(llm_client, "call_model",
                        lambda m, p, **kw: seen.append(m) or "ok")

    llm_client.call_role("screener", "ping")

    assert seen == [llm_client.ROLE_DEFAULTS["screener"]]


def test_an_unknown_role_is_loud(seen):
    """角色名写错时要报错，不能悄悄回落到某个默认模型。"""
    with pytest.raises(llm_client.UnknownRole) as exc:
        llm_client.call_role("screner", "ping")

    assert "screner" in str(exc.value)
    assert "screener" in str(exc.value), "要把正确的名字列出来"
    assert not seen


def test_the_screen_and_judge_call_sites_go_through_call_role():
    """两个调用点原来直接调 call_flash / call_pro，preset 写死在函数里。"""
    import inspect

    import pipeline_v4

    for func in (pipeline_v4.llm_insight_filter, pipeline_v4.final_pro_judgment):
        source = inspect.getsource(func)
        assert "call_role(" in source, f"{func.__name__} 没走 call_role"
        assert "call_flash(" not in source and "call_pro(" not in source


# ---- 检查器认得的名字，运行时也要认 ----

def test_every_model_preflight_knows_can_be_dispatched(monkeypatch):
    """`AR_MODEL_JUDGE=oai-mid` 让 preflight 变绿、运行时抛「不认识」，是最贵的一种假绿。

    本机实测过：providers 认得 18 个模型，其中 13 个 `call_model` 解析不出来——它只看
    `config.local.json` 的 presets 和一张写死的表。判据是「每个名字都到得了传输层」，
    不是「有个函数叫这个名」，所以桩打在真正发请求的那一层上。
    """
    import providers

    sent = []
    monkeypatch.setattr(providers, "send",
                        lambda wire, dialect: sent.append(wire) or providers.Reply(
                            providers.DELIVERED, text="ok"))

    config = providers.load_effective_config(REPO)[0]
    declared = sorted(providers.as_profiles(config))
    assert declared, "配置里一个模型都没有，这条门就是空的"

    unknown = []
    for name in declared:
        try:
            llm_client.call_model(name, "hi")
        except llm_client.UnknownModel:
            unknown.append(name)
        except Exception:
            # 别的异常说明它找到了路并且试过了（多半是本机没配那份凭据），
            # 这条门问的是「认不认得这个名字」。
            pass

    assert not unknown, f"preflight 认得但运行时不认得：{unknown}"


def test_a_name_only_the_providers_config_knows_reaches_the_wire(monkeypatch):
    """上一条容忍其它异常，所以还要有一条真看到请求出去。"""
    import providers

    sent = []
    monkeypatch.setattr(providers, "send",
                        lambda wire, dialect: sent.append(wire) or providers.Reply(
                            providers.DELIVERED, text="ok"))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    config = providers.load_effective_config(REPO)[0]
    only_providers = [n for n in providers.as_profiles(config)
                      if n not in llm_client._LEGACY and not llm_client._preset_for(n)[1]]
    assert only_providers, "没有这样的名字，这条门是空的"

    assert llm_client.call_model(only_providers[0], "hi") == "ok"
    assert sent, "没有到达传输层"


def test_a_name_nobody_declares_still_says_so(monkeypatch):
    """兜底不能变成「什么名字都收下」。"""
    with pytest.raises(llm_client.UnknownModel) as exc:
        llm_client.call_model("没有人声明过这个名字", "hi")

    assert "没有配置能提供" in str(exc.value)


def test_the_error_lists_the_names_that_actually_work():
    """报错要能照着做。之前它只列 presets，漏掉 providers 里的一多半。"""
    import providers

    with pytest.raises(llm_client.UnknownModel) as exc:
        llm_client.call_model("不存在", "hi")

    declared = set(providers.as_profiles(providers.load_effective_config(REPO)[0]))
    listed = str(exc.value)
    assert declared <= {part.strip() for line in listed.splitlines()
                        for part in line.split("：")[-1].split(",")}


# ---- 有没有这个角色，同样以配置为准 ----

def test_every_role_the_config_declares_is_known_at_runtime(seen):
    """配置声明、preflight 逐个探过的角色，运行时问它要模型时不能答「没有这个角色」。

    `run_monitor` 就是这么漏的：它在 `config/providers.example.json` 里有候选、
    `preflight.py --role run_monitor` 也在探，而运行时那份角色表是代码里的
    `ROLE_DEFAULTS`，六个名字里没有它。monitor 把异常吞成一行 `summary unavailable`，
    于是每台机器上的每段记录都少一句摘要，而没有任何一处报红（#191）。

    这是这一版清了一整轮的那个形状：门和被门管的东西读的不是同一处。
    """
    import providers

    config = providers.load_effective_config(REPO)[0]
    declared = [name for name in config.get("roles", {})
                if not name.startswith("_") and providers.role_candidates(config, name)]
    assert declared, "配置里一个角色都没读到，这条断言就没意义了"

    unknown = []
    for role in declared:
        try:
            llm_client.call_role(role, "hi")
        except llm_client.UnknownRole:
            unknown.append(role)

    assert not unknown, f"配置声明了这些角色，运行时却不认得：{unknown}"


def test_a_role_nobody_declared_still_says_so(monkeypatch):
    """反向：写错的角色名要立刻说，不能因为「以配置为准」就变成什么名字都收。"""
    with pytest.raises(llm_client.UnknownRole, match="没有叫 typo_role 的角色"):
        llm_client.call_role("typo_role", "hi")


def test_the_error_lists_the_roles_the_config_declares(monkeypatch):
    """报错要能照着改。只列代码里那张表的话，配置里多出来的角色一个都不会出现。"""
    with pytest.raises(llm_client.UnknownRole) as exc:
        llm_client.call_role("typo_role", "hi")

    assert "run_monitor" in str(exc.value)


# ---- 回落属于配置，不属于代码里那张表 ----

def test_a_role_falls_back_to_its_next_candidate(monkeypatch):
    """配置给每个角色写了三到五个候选，preflight 也逐个探过。

    运行时原来只取 `[0]`，首选挂掉整个角色就没了，尽管配置里还有备选。`_LEGACY` 里
    那几条写死的回落链就是在补这个洞——一份写在代码里、只覆盖六个名字的回落表。
    """
    tried = []

    def dispatch(model, prompt, **kw):
        tried.append(model)
        return "第二个给的" if len(tried) > 1 else None

    monkeypatch.setattr(llm_client, "call_model", dispatch)
    monkeypatch.setattr(roles, "models_for", lambda role, default: ["首选", "备选"])

    assert llm_client.call_role("judge", "hi") == "第二个给的"
    assert tried == ["首选", "备选"]


def test_an_exception_from_one_candidate_does_not_end_the_role(monkeypatch):
    """端点挂了和模型没话说是两回事，前者该换下一个候选继续。"""
    def dispatch(model, prompt, **kw):
        if model == "首选":
            raise RuntimeError("endpoint down")
        return "备选给的"

    monkeypatch.setattr(llm_client, "call_model", dispatch)
    monkeypatch.setattr(roles, "models_for", lambda role, default: ["首选", "备选"])

    assert llm_client.call_role("judge", "hi") == "备选给的"


def test_an_unknown_model_is_not_swallowed_by_the_fallback(monkeypatch):
    """名字打错要立刻说，不能被「再试下一个」盖住——下一个可能恰好能用，于是错误配置
    一直跑着而没人知道。"""
    def dispatch(model, prompt, **kw):
        raise llm_client.UnknownModel(f"没有 {model}")

    monkeypatch.setattr(llm_client, "call_model", dispatch)
    monkeypatch.setattr(roles, "models_for", lambda role, default: ["打错的", "能用的"])

    with pytest.raises(llm_client.UnknownModel):
        llm_client.call_role("judge", "hi")


def test_a_single_candidate_that_raises_surfaces_the_reason(monkeypatch):
    """吞掉异常等于把「端点挂了」说成「模型没话说」，下游按后者处理。"""
    def dispatch(model, prompt, **kw):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(llm_client, "call_model", dispatch)
    monkeypatch.setattr(roles, "models_for", lambda role, default: ["唯一的"])

    with pytest.raises(RuntimeError, match="endpoint down"):
        llm_client.call_role("judge", "hi")


def test_every_candidate_failing_returns_nothing(monkeypatch):
    """全挂时返回 None，跟其余 call_* 的约定一致。"""
    monkeypatch.setattr(llm_client, "call_model", lambda *a, **kw: None)
    monkeypatch.setattr(roles, "models_for", lambda role, default: ["a", "b", "c"])

    assert llm_client.call_role("judge", "hi") is None


def test_the_first_candidate_is_still_preferred(monkeypatch):
    """回落是兜底，不是轮询：首选能用时不该碰第二个。"""
    tried = []
    monkeypatch.setattr(llm_client, "call_model",
                        lambda m, p, **kw: tried.append(m) or "首选给的")
    monkeypatch.setattr(roles, "models_for", lambda role, default: ["首选", "备选"])

    assert llm_client.call_role("judge", "hi") == "首选给的"
    assert tried == ["首选"]
