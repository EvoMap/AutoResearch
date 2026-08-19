"""请求怎么造、结果怎么分类，是纯函数，所以能不开 socket 地钉住。

这正是 `build / send / read` 分三步的理由：`build` 不发送、`read` 只认
`.status_code` / `.text` / `.json()`，于是「发出去的字节一个都没变」是一句可断言的话。

结果词汇比「成功 / 失败」细，因为下游要据此做不同的事。原来 `call_claude` 对六种不同原因
都返回 `None`，而且一个正常的空文本块也返回 falsy，七种情况长得一模一样：`pipeline_v4`
把端点挂了记成「模型写了读不懂的东西」，`forge` 把它当成一张弃权票，于是一个模型静默失败
会**抬高**面板的 ≥2 票门槛。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import providers  # noqa: E402

OPENAI = providers.DIALECTS["openai_chat"]
ANTHROPIC = providers.DIALECTS["anthropic_messages"]
ENDPOINT = {"base_url": "https://example.invalid/v1", "api_key": "sk-test"}
GATEWAY = {"base_url": "https://gw.invalid", "auth_token": "tok-test"}


class FakeResponse:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text if payload is None else "<json>"
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# ---- build：请求长什么样 ----

@pytest.mark.parametrize("base,expected", [
    ("https://x/v1", "https://x/v1/chat/completions"),
    ("https://x", "https://x/v1/chat/completions"),
    ("https://x/v1/", "https://x/v1/chat/completions"),
    ("https://x/v1/chat/completions", "https://x/v1/chat/completions"),
])
def test_the_chat_url_is_idempotent(base, expected):
    """原来两种拼法各写一遍，于是同一个端点要在配置里声明用哪种。幂等之后这个维度没了。"""
    assert OPENAI.url(base) == expected


def test_a_deployment_that_refuses_temperature_does_not_get_one():
    """同一个 Azure 端点上，一个模型收 temperature、另一个拒。这是绑定的属性，不是厂商的。"""
    with_temp = OPENAI.build(ENDPOINT, "m", "hi", {})
    without = OPENAI.build(ENDPOINT, "m", "hi", {"supports_temperature": False})

    assert "temperature" in with_temp.body
    assert "temperature" not in without.body


def test_the_token_field_name_is_a_parameter():
    """`gpt-5.6-sol` 要 `max_completion_tokens`，同端点的 `Mistral-Large-3` 要 `max_tokens`。"""
    wire = OPENAI.build(ENDPOINT, "m", "hi", {"token_param": "max_completion_tokens"})

    assert "max_completion_tokens" in wire.body and "max_tokens" not in wire.body


def test_an_openai_route_can_disable_reasoning_without_changing_other_routes():
    default = OPENAI.build(ENDPOINT, "m", "hi", {})
    without_reasoning = OPENAI.build(
        ENDPOINT,
        "m",
        "hi",
        {"reasoning_effort": "none"},
    )

    assert "reasoning_effort" not in default.body
    assert without_reasoning.body["reasoning_effort"] == "none"


@pytest.mark.parametrize("value", [None, "", "off", 0, True])
def test_openai_reasoning_effort_rejects_unsupported_values(value):
    with pytest.raises(ValueError, match="reasoning_effort"):
        OPENAI.build(ENDPOINT, "m", "hi", {"reasoning_effort": value})


def test_request_budget_has_one_global_default_and_optional_role_override():
    config = {
        "request_defaults": {"max_tokens": 8192},
        "roles": {
            "planner": {"models": ["m"], "request": {"max_tokens": 12288}},
            "judge": {"models": ["m"]},
        },
    }

    assert providers.request_params(config) == {"max_tokens": 8192}
    assert providers.request_params(config, "judge") == {"max_tokens": 8192}
    assert providers.request_params(config, "planner") == {"max_tokens": 12288}


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8192"])
def test_request_budget_rejects_non_positive_integers(value):
    with pytest.raises(ValueError, match="max_tokens"):
        providers.request_params({"request_defaults": {"max_tokens": value}})


def test_dispatch_preserves_route_parameters_and_url_strategy(monkeypatch):
    config = {
        "version": 2,
        "endpoints": {
            "gateway": {
                "dialect": "openai_chat",
                "base_url": "https://gateway.invalid/openai",
                "credential_env": ["GATEWAY_KEY"],
            },
        },
        "models": {
            "reasoner": {
                "routes": [{
                    "endpoint": "gateway",
                    "wire_name": "reasoner-v1",
                    "params": {
                        "token_param": "max_completion_tokens",
                        "supports_temperature": False,
                        "url_style": "append",
                        "reasoning_effort": "none",
                    },
                }],
            },
        },
    }
    sent = []
    monkeypatch.setattr(
        providers,
        "send",
        lambda wire, _dialect: sent.append(wire)
        or providers.Reply(providers.DELIVERED, text="ok"),
    )

    providers.dispatch(
        config,
        "reasoner",
        "hello",
        env={"GATEWAY_KEY": "secret"},
        max_tokens=1000,
        temperature=0.2,
    )

    assert sent[0].url == "https://gateway.invalid/openai/chat/completions"
    assert sent[0].body["max_completion_tokens"] == 1000
    assert "max_tokens" not in sent[0].body
    assert "temperature" not in sent[0].body
    assert sent[0].body["reasoning_effort"] == "none"


def test_tracked_google_route_uses_the_url_consumed_by_the_api(monkeypatch):
    config = json.loads(
        (REPO / "config" / "providers.example.json").read_text(encoding="utf-8"))
    sent = []
    monkeypatch.setattr(
        providers,
        "send",
        lambda wire, _dialect: sent.append(wire)
        or providers.Reply(providers.DELIVERED, text="ok"),
    )

    providers.dispatch(
        config,
        "gemini-3.1-pro",
        "hello",
        env={"GEMINI_API_KEY": "secret"},
    )

    assert sent[0].url == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")


def test_anthropic_sends_both_credential_headers():
    """官方认 x-api-key，部分兼容网关只认 Bearer。少发一个就有一类端点被误判成鉴权失败。"""
    wire = ANTHROPIC.build(GATEWAY, "m", "hi", {})

    assert wire.headers["x-api-key"] == "tok-test"
    assert wire.headers["Authorization"] == "Bearer tok-test"
    assert wire.url == "https://gw.invalid/v1/messages"


def test_credentials_never_reach_a_snapshot():
    """脱敏后的头要能直接进日志和 golden 快照。"""
    redacted = ANTHROPIC.build(GATEWAY, "m", "hi", {}).redacted_headers()

    assert "tok-test" not in str(redacted)
    assert redacted["x-api-key"].endswith("chars>")


# ---- read：结果怎么分类 ----

def test_text_comes_back_as_delivered():
    reply = OPENAI.read(FakeResponse(payload={
        "choices": [{"message": {"content": " OK "}, "finish_reason": "stop"}]}), 0.1)

    assert reply.outcome == providers.DELIVERED and reply.ok
    assert reply.text == " OK " and reply.finish_reason == "stop"


def test_an_empty_answer_is_not_the_same_as_a_dead_endpoint():
    """推理模型把预算耗在隐式思考上时，200 是真的、文本是真的空。"""
    reply = OPENAI.read(FakeResponse(payload={"choices": [{"message": {"content": ""}}]}), 0.1)

    assert reply.outcome == providers.EMPTY
    assert not reply.ok and not reply.retriable


def test_a_shape_we_were_not_promised_is_unreadable():
    assert OPENAI.read(FakeResponse(payload={"nope": 1}), 0.1).outcome == providers.UNREADABLE
    assert OPENAI.read(FakeResponse(text="<html>"), 0.1).outcome == providers.UNREADABLE


def test_anthropic_thinking_only_is_empty_not_a_failure():
    """只有 thinking / tool_use 块时，原来返回 None，和网络挂了长得一模一样。"""
    reply = ANTHROPIC.read(FakeResponse(payload={
        "content": [{"type": "thinking", "thinking": "..."}], "stop_reason": "end_turn"}), 0.1)

    assert reply.outcome == providers.EMPTY
    assert "thinking" in reply.detail


@pytest.mark.parametrize("status,outcome,retriable", [
    (400, providers.REJECTED, False),
    (401, providers.REJECTED, False),
    (403, providers.REJECTED, False),
    (429, providers.THROTTLED, True),
    (500, providers.REMOTE_ERROR, True),
    (503, providers.REMOTE_ERROR, True),
])
def test_non_200_splits_by_what_to_do_about_it(status, outcome, retriable):
    """一个 403 重试三次只是白等 10 秒；一个 429 不等 Retry-After 只会再撞一次。"""
    reply = OPENAI.read(FakeResponse(status=status, payload={
        "error": {"message": "nope", "code": "some_code"}}), 0.1)

    assert reply.outcome == outcome
    assert reply.retriable is retriable
    assert reply.provider_code == "some_code"
    assert reply.http_status == status


def test_retry_after_is_carried_not_interpreted():
    """outcome 说发生了什么，policy 说怎么办——所以这里只负责把它带出来。"""
    reply = OPENAI.read(FakeResponse(status=429, payload={}, headers={"retry-after": "30"}), 0.1)

    assert reply.retry_after == "30"


def test_the_outcome_vocabulary_has_no_catch_all():
    """`REFUSED` 那种粗分类会把鉴权拒绝、参数错误、429 和 5xx 压成一个结论。"""
    distinct = {providers.DELIVERED, providers.EMPTY, providers.UNREADABLE,
                providers.REJECTED, providers.THROTTLED, providers.REMOTE_ERROR,
                providers.UNREACHABLE, providers.UNCONFIGURED}

    assert len(distinct) == 8
    assert providers.RETRIABLE < distinct


# ---- send：传输层 ----

def test_an_unavailable_required_proxy_is_unreachable_not_rejected(monkeypatch):
    """代理不通时端点根本没答话，不能记成它拒绝了我们。"""
    monkeypatch.setattr(providers.proxy_contract, "effective_kwargs",
                        lambda *a, **kw: None)

    reply = providers.call(OPENAI, ENDPOINT, "m")

    assert reply.outcome == providers.UNREACHABLE and reply.stage == "transport"


def test_a_transport_exception_is_unreachable(monkeypatch):
    class Boom:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, *a, **kw): raise OSError("connection reset")

    monkeypatch.setattr(providers.proxy_contract, "effective_kwargs",
                        lambda *a, **kw: {"timeout": 1})
    monkeypatch.setattr(providers.httpx, "Client", Boom)

    reply = providers.call(OPENAI, ENDPOINT, "m")

    assert reply.outcome == providers.UNREACHABLE
    assert "connection reset" in reply.detail


# ---- v1 local 盖在 v2 default 上：所有旧机器升级后的状态 ----

V1_LOCAL = {
    "version": 1,
    "profiles": {
        "azure-gpt-5.6": {"api": "openai_chat", "base_url_env": "AZURE_OPENAI_ENDPOINT",
                          "api_key_env": "AZURE_OPENAI_API_KEY", "model": "gpt-5.6-sol"},
        "claude-opus": {"api": "anthropic_messages", "base_url_env": "ANTHROPIC_BASE_URL",
                        "auth_token_env": "ANTHROPIC_API_KEY", "model": "本机改过的名字"},
    },
    "roles": {"ideator": {"candidates": ["azure-gpt-5.6", "claude-opus"]}},
}

V2_DEFAULT = {
    "version": 2,
    "endpoints": {"anthropic-compatible": {
        "dialect": "anthropic_messages", "base_url_env": "ANTHROPIC_BASE_URL",
        "credential_env": ["ANTHROPIC_API_KEY"], "url_env_fallback": []}},
    "models": {"claude-opus": {"capabilities": {}, "routes": [
        {"endpoint": "anthropic-compatible", "wire_name": "上游的默认名字"}]}},
    "roles": {"ideator": {"models": ["claude-opus"]},
              "screener": {"models": ["claude-opus"]}},
}


def test_a_v1_local_still_wins_over_a_v2_default():
    """tracked 换成 v2 之后，本机那份 v1 不能就此失效。

    按 section 合并会得到「local 的 profiles + default 的 endpoints」，`is_v2` 看到
    endpoints 就为真，于是 `as_profiles` 只读 models：本机独有的 profile 整个消失，
    同名的被上游的值顶替。实测过一台机器的 Azure 配置就这么没了，一声不吭。
    """
    merged, _ = providers.merge_over_defaults(V1_LOCAL, V2_DEFAULT)
    flat = providers.as_profiles(merged)

    assert "azure-gpt-5.6" in flat, "本机独有的 profile 不能消失"
    assert flat["claude-opus"]["model"] == "本机改过的名字", "本机改过的值不能被上游顶替"


def test_the_defaults_still_fill_in_what_the_local_file_never_had():
    """合并的另一半：上游新加的角色要能到达老机器。"""
    merged, added = providers.merge_over_defaults(V1_LOCAL, V2_DEFAULT)

    assert providers.role_candidates(merged, "screener") == ["claude-opus"]
    assert any("screener" in line for line in added), "补了什么要说出来"
    assert providers.role_candidates(merged, "ideator") == ["azure-gpt-5.6", "claude-opus"], \
        "本机写过的角色原样保留"


def test_the_same_format_on_both_sides_keeps_the_richer_shape():
    """两边都是 v2 时不该退化成摊平的 v1。"""
    local = {"version": 2, "endpoints": {}, "models": {}, "roles": {}}

    merged, _ = providers.merge_over_defaults(local, V2_DEFAULT)

    assert providers.is_v2(merged)
    assert "routes" in merged["models"]["claude-opus"]


# ---- Supported configuration formats ----

def test_a_config_carrying_v1_shapes_is_detectable():
    """v1 profile and preset inputs remain detectable at the compatibility boundary."""
    assert providers.uses_v1({"profiles": {"x": {}}})
    assert providers.uses_v1({"presets": {"x": {}}})
    assert not providers.uses_v1({"version": 2, "endpoints": {}, "models": {}})


def test_the_compatibility_contract_names_both_formats():
    window = providers.COMPATIBILITY_WINDOW

    assert "as_profiles" in window and "_merge_across_formats" in window
    assert "v1" in window and "v2" in window


# ---- 重试执行器：每次尝试都要留痕 ----

def replies(*outcomes, retry_after=""):
    queue = [providers.Reply(o, retry_after=retry_after if o == providers.THROTTLED else "")
             for o in outcomes]
    return lambda: queue.pop(0)


def test_a_transient_failure_is_retried_and_every_attempt_is_recorded():
    """`Reply.attempts` 一直存在却没人写：外层只看得到最后一次，撞了几次限流不可见。"""
    waits = []

    reply = providers.with_retries(
        replies(providers.THROTTLED, providers.REMOTE_ERROR, providers.DELIVERED),
        sleep=waits.append)

    assert reply.outcome == providers.DELIVERED
    assert [a["outcome"] for a in reply.attempts] == [
        providers.THROTTLED, providers.REMOTE_ERROR, providers.DELIVERED]
    assert len(waits) == 2, "两次失败之间要等，最后一次成功后不等"


def test_a_geo_block_is_never_retried():
    """执行位置固定之后它意味着环境漂移，重试只会把漂移藏起来。"""
    waits = []

    reply = providers.with_retries(replies(providers.GEO_BLOCKED), sleep=waits.append)

    assert len(reply.attempts) == 1 and not waits


def test_a_rejection_is_never_retried():
    """鉴权或参数错，重发一万次也一样。"""
    calls = []

    def once():
        calls.append(1)
        return providers.Reply(providers.REJECTED, http_status=401)

    providers.with_retries(once, sleep=lambda _: None)

    assert len(calls) == 1


def test_the_limit_is_respected_and_the_last_failure_is_returned():
    waits = []

    reply = providers.with_retries(
        replies(*[providers.THROTTLED] * 5), limit=3, sleep=waits.append)

    assert reply.outcome == providers.THROTTLED
    assert len(reply.attempts) == 3
    assert len(waits) == 2, "到头的那次不该再等"


def test_retry_after_wins_over_backoff():
    """对面说了等多久就等多久：自己算一个更短的只会再撞一次。"""
    waits = []

    providers.with_retries(
        replies(providers.THROTTLED, providers.DELIVERED, retry_after="7"),
        sleep=waits.append)

    assert waits == [7.0]


def test_a_nonsense_retry_after_falls_back_to_backoff():
    """HTTP 日期形式和垃圾值都不该让退避崩掉。"""
    assert providers.retry_delay(
        providers.Reply(providers.THROTTLED, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"),
        attempt=1, base=1.0) == 2.0


def test_the_backoff_has_a_ceiling():
    assert providers.retry_delay(
        providers.Reply(providers.THROTTLED), attempt=20, base=1.0, ceiling=30.0) == 30.0
    assert providers.retry_delay(
        providers.Reply(providers.THROTTLED, retry_after="99999"),
        attempt=0, ceiling=30.0) == 30.0


def test_concurrency_defaults_to_serial_and_accepts_a_positive_limit():
    assert providers.max_concurrency({}) == 1
    assert providers.max_concurrency({"execution": {"max_concurrency": 6}}) == 6


@pytest.mark.parametrize("bad", [True, 0, -1, 1.5, "6"])
def test_invalid_concurrency_is_rejected(bad):
    with pytest.raises(ValueError, match="execution.max_concurrency"):
        providers.max_concurrency({"execution": {"max_concurrency": bad}})


