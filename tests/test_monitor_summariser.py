"""monitor 的摘要走 OpenAI 兼容端点，不再只有 Vertex 一条路。

`ar-gemini-monitor.py` 是 AutoResearch 侧最后一个 Vertex 消费者：缺
`GEMINI_VERTEX_SERVICE_ACCOUNT` 或 `GEMINI_VERTEX_PROJECT_ID` 时 `call_gemini` 直接返回
空串，于是 monitor 静默降级成只有心跳、没有摘要，而 Vertex 已经决定退役（#73）。

它跑在跟流水线同一个解释器里，所以直接用 `llm_client.call_role("run_monitor", ...)`：
换模型的旋钮、preflight 报的模型、真正发出去的模型，从此是同一个。

「缺凭据时返回空串」这个行为要保留——monitor 的心跳、idle/stale 检测不依赖模型，摘要拿不到
就该少报一段，而不是让整个 monitor 起不来。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

MONITOR = REPO / "ar-runtime" / "scripts" / "ar-gemini-monitor.py"


def load_monitor():
    spec = importlib.util.spec_from_file_location("ar_monitor", MONITOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def monitor():
    return load_monitor()


def test_the_summary_goes_through_the_role_entry(monitor, monkeypatch):
    """判据是「派发器收到了什么」，不是「有个函数叫这个名」。"""
    seen = {}
    import llm_client

    monkeypatch.setattr(llm_client, "call_role",
                        lambda role, prompt, **kw: seen.update(role=role, prompt=prompt) or "跑得挺顺")

    assert monitor.summarise("epoch 3 loss=0.21") == "跑得挺顺"
    assert seen["role"] == "run_monitor"
    assert "epoch 3 loss=0.21" in seen["prompt"]


def test_the_role_resolves_without_stubbing_the_role_entry(monitor, monkeypatch):
    """上面那条把 `call_role` 换成了桩，于是「运行时认不认得 run_monitor」从来没被问过。

    答案是不认得：这个角色写在配置里、preflight 也在探，而运行时的角色表是代码里的
    `ROLE_DEFAULTS`。`call_role` 抛 UnknownRole，下面那条「失败就少报一段」的宽容正好
    把它吞掉，于是每台机器上的摘要都是空的，而没有任何一处报红（#191）。

    所以这里只挡住派发器，角色解析走真的那一份。
    """
    import llm_client

    monkeypatch.setattr(llm_client, "call_model", lambda name, prompt, **kw: "跑得挺顺")

    assert monitor.summarise("epoch 3 loss=0.21") == "跑得挺顺"


def test_a_failure_degrades_to_no_summary_not_a_crash(monitor, monkeypatch):
    """心跳和 idle/stale 不依赖模型，摘要拿不到就少报一段。"""
    import llm_client

    def boom(*a, **kw):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(llm_client, "call_role", boom)

    assert monitor.summarise("whatever") == ""


def test_an_empty_answer_is_not_an_exception(monitor, monkeypatch):
    import llm_client

    monkeypatch.setattr(llm_client, "call_role", lambda *a, **kw: None)

    assert monitor.summarise("whatever") == ""


def test_no_vertex_left_in_the_monitor():
    """AutoResearch 侧不再生成、推荐和检查 Vertex 配置。"""
    body = MONITOR.read_text(encoding="utf-8")

    for gone in ("VERTEX_SERVICE_ACCOUNT", "aiplatform.googleapis.com",
                 "service_account", "generateContent"):
        assert gone not in body, f"monitor 里还留着 {gone}"


def test_the_prompt_still_asks_for_one_paragraph(monitor, monkeypatch):
    """摘要格式是 notifications.log 的消费契约，迁移不该改它。"""
    seen = {}
    import llm_client

    monkeypatch.setattr(llm_client, "call_role",
                        lambda role, prompt, **kw: seen.update(prompt=prompt) or "x")
    monitor.summarise("log")

    assert "ONE paragraph" in seen["prompt"] and "progress: routine" in seen["prompt"]
