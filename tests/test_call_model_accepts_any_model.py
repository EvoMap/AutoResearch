"""preflight 说这个角色能用某个模型，运行时就得真能调它。

`AR_MODEL_JUDGE=<任何模型>` 让 preflight 探到那个模型并变绿，而 `call_model` 是一条写死
六个名字的 if 链，第七个名字掉进 `else: print("未知模型"); return None`。于是：检查器绿、
跑起来这一步静默返回 None。这正是「报告说用 A、实际跑 B」的那种分叉，而且实际什么
都没跑，而调用方拿到的 None 会被当成「模型没话说」。

所以派发不能按名字枚举，要按配置：名字命中 preset 的 key 或它的 model 字段，就照那个
preset 的 provider 发出去。配置里没有的名字必须响亮地失败，并说清有哪些可用。静默返回
None 比报错难查得多。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import llm_client  # noqa: E402
import providers  # noqa: E402

PRESETS = {
    "my-chat-preset": {
        "provider": "openai_chat_completions",
        "base_url": "https://example.invalid/v1",
        "api_key": "k",
        "model": "some-chat-model",
    },
    "my-claude-preset": {
        "provider": "anthropic_messages",
        "base_url": "https://example.invalid",
        "auth_token": "k",
        "model": "some-claude-model",
    },
}


@pytest.fixture
def sent(monkeypatch):
    """记下真正发出去的 payload，不发网络。"""
    seen: list[dict] = []
    monkeypatch.setattr(llm_client, "load_legacy_config", lambda: {"presets": PRESETS})

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "hi"}}],
                    "content": [{"type": "text", "text": "hi"}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            seen.append({"url": url, "json": json})
            return Response()

    monkeypatch.setattr(llm_client.httpx, "Client", Client)
    return seen


def test_a_preset_key_is_callable_by_name(sent):
    assert llm_client.call_model("my-chat-preset", "ping") == "hi"

    assert sent[0]["json"]["model"] == "some-chat-model"


def test_a_model_name_reaches_its_preset(sent):
    """人写在 AR_MODEL_<ROLE> 里的是模型名，不是我们内部的 preset 名。"""
    assert llm_client.call_model("some-chat-model", "ping") == "hi"

    assert sent[0]["json"]["model"] == "some-chat-model"


def test_an_anthropic_preset_goes_to_the_messages_endpoint(sent):
    assert llm_client.call_model("some-claude-model", "ping") == "hi"

    assert sent[0]["url"].endswith("/v1/messages")
    assert sent[0]["json"]["model"] == "some-claude-model"


def test_an_unknown_model_fails_loudly(sent, capsys):
    """静默 None 会被当成「模型没话说」，一路流到判定为空。"""
    with pytest.raises(llm_client.UnknownModel) as exc:
        llm_client.call_model("not-in-any-preset", "ping")

    message = str(exc.value)
    assert "not-in-any-preset" in message
    assert "some-chat-model" in message, "要告诉人现在有哪些名字可用"
    assert not sent, "认不出来的名字不该发请求"


def test_an_unshadowed_legacy_name_still_works(sent, monkeypatch):
    """统一配置没声明的旧名字在迁移期仍可使用。"""
    called = []
    monkeypatch.setattr(llm_client, "call_flash", lambda p, **k: called.append("flash") or "hi")

    assert llm_client.call_model("gemini-flash", "ping") == "hi"
    assert called == ["flash"]


# ---- 名字解析得出来、这次发不出去，是另一回事 ----

def providers_reply(outcome, **fields):
    return providers.Reply(outcome=outcome, **fields)


@pytest.fixture
def dispatched(monkeypatch):
    """providers 配置里有 `cfg-model` 这个名字，dispatch 的结果由用例给。"""
    monkeypatch.setattr(
        llm_client,
        "_provider_config",
        lambda: {
            "request_defaults": {"max_tokens": 8192},
            "models": {"cfg-model": {}},
        },
    )
    monkeypatch.setattr(providers, "as_profiles", lambda config: {"cfg-model": {}})

    def use(reply):
        monkeypatch.setattr(providers, "dispatch",
                            lambda config, model, prompt, **params: reply)

    return use


def test_an_endpoint_that_cannot_be_reached_is_not_a_missing_model(sent, dispatched):
    """DNS 不通、401、502 都是这次请求的事，不是「配置里没有这个模型」。

    两件事原来共用一个 `None`，于是报错说模型不存在，下一行又把它列进「现在可用」，
    而真正的原因（`Reply.detail`）被丢掉，排错方向被带去查配置（#194）。
    """
    dispatched(providers_reply(
        providers.UNREACHABLE, stage="transport",
        detail="ConnectError: [Errno 8] nodename nor servname provided"))

    with pytest.raises(RuntimeError) as exc:      # UnknownModel 是 ValueError，不在这里
        llm_client.call_model("cfg-model", "ping")

    message = str(exc.value)
    assert "cfg-model" in message
    assert "ConnectError" in message, "真正的原因要交出去，不能只说一句失败"


def test_a_seat_that_cannot_be_reached_costs_one_candidate(sent, dispatched, monkeypatch):
    """判据是角色还答不答得出来。

    `UnknownModel` 那一档的约定是「配置写错了，换个候选也救不了」，所以 `call_role` 原样
    抛、构思席位循环也原样抛。把端点连不上归到这一档，一个连不上的席位就会带走整轮。
    """
    dispatched(providers_reply(providers.UNREACHABLE, stage="transport", detail="ConnectError"))
    monkeypatch.setattr(llm_client, "_configured_candidates",
                        lambda role, config=None: ["cfg-model", "my-chat-preset"])

    assert llm_client.call_role("judge", "ping") == "hi"


def test_a_model_with_nothing_to_say_is_still_nothing_to_say(sent, dispatched):
    """`empty` 是 200、形状对、文本为空，表示模型没话说，与发不出去含义不同。

    preset 那条路遇到空内容返回 None，这条路要一致：调用方按 None 换下一个候选。
    """
    dispatched(providers_reply(providers.EMPTY, detail="no content"))

    assert llm_client.call_model("cfg-model", "ping") is None
