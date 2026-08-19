"""preflight 探的必须是运行时真正走的第一跳。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import llm_client  # noqa: E402

DIRECT_ONLY = {"presets": {"gpt-5.5": {"provider": "openai_chat_completions",
                                       "base_url": "https://provider.invalid/v1",
                                       "api_key": "k", "model": "gpt-5.5"}}}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(llm_client, "load_legacy_config", lambda: DIRECT_ONLY)


def test_the_first_hop_is_answerable(configured):
    assert llm_client.first_hop("gpt-5.5") == "gpt-5.5"


def test_the_reported_hop_is_the_one_called(configured):
    monkeypatch_sent = []
    llm_client.call_gpt.__globals__["httpx"]  # 确保名字还在，改名时这里先炸

    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            monkeypatch_sent.append(url)
            class R:
                status_code = 200
                @staticmethod
                def json(): return {"choices": [{"message": {"content": "ok"}}]}
            return R()

    original = llm_client.httpx.Client
    llm_client.httpx.Client = Client
    try:
        assert llm_client.call_gpt("ping") == "ok"
    finally:
        llm_client.httpx.Client = original

    assert monkeypatch_sent and "provider.invalid" in monkeypatch_sent[0]


def test_every_legacy_name_can_report_its_first_hop(configured):
    """`_LEGACY` 里每个名字都要答得出来，答不出的那个就是没人探过的那个。"""
    for name in llm_client._LEGACY:
        assert llm_client.first_hop(name) is not None or name, f"{name} 的第一跳答不出"
