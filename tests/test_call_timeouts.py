"""构思一次要多久，决定超时该给多少。

Step 1 的提示词把整份领域知识文件注进去（`knowledge_base/` 里单份 10–24KB），再要
2500 token 的输出。实测 Mistral-Large-3 在一个短得多的提示词上就要 59.4 秒，而
`call_gpt` 写死 120 秒——2026-08-10 的第一次真实跑批里，三个构思席位有一个就这么超时
掉了，日志只留下一句「The read operation timed out」。

超时不是越大越好：调用挂住时它决定操作者要等多久才看到失败。所以给默认值，同时留一个
环境变量，让慢端点上的人不用改代码。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


@pytest.fixture
def client(monkeypatch):
    def load():
        import importlib

        import llm_client
        return importlib.reload(llm_client)
    return load


def test_the_default_leaves_room_for_a_knowledge_sized_prompt(client, monkeypatch):
    """120 秒不够：实测一个远小于构思提示词的请求就要 59.4 秒。"""
    monkeypatch.delenv("AR_LLM_TIMEOUT", raising=False)

    assert client().LLM_TIMEOUT == 900


def test_the_timeout_is_configurable(client, monkeypatch):
    """慢端点上不该逼人改代码。"""
    monkeypatch.setenv("AR_LLM_TIMEOUT", "45")

    assert client().LLM_TIMEOUT == 45


def test_a_bad_value_falls_back_instead_of_crashing_the_run(client, monkeypatch):
    """这个变量会被写进 .env，写错一个字不该让整条管线起不来。"""
    monkeypatch.setenv("AR_LLM_TIMEOUT", "not-a-number")

    assert client().LLM_TIMEOUT == 900


def test_every_call_path_uses_it(client, monkeypatch):
    """三个构思席位用同一个预算。留一处写死，那个席位就会单独超时掉。"""
    import inspect

    module = client()
    source = inspect.getsource(module)
    for hardcoded in ("timeout=120", "timeout=180"):
        assert hardcoded not in source, f"还有写死的 {hardcoded}"


def test_unified_provider_dispatch_receives_the_configured_timeout(client, monkeypatch):
    module = client()
    monkeypatch.setattr(module, "LLM_TIMEOUT", 321)
    seen = {}

    def dispatch(*_args, **kwargs):
        seen.update(kwargs)
        return module.providers.Reply(module.providers.DELIVERED, text="ok")

    monkeypatch.setattr(module.providers, "dispatch", dispatch)
    result = module._via_providers(
        "model", "prompt", config={"request_defaults": {"max_tokens": 8192}})

    assert result == "ok"
    assert seen["timeout"] == 321
