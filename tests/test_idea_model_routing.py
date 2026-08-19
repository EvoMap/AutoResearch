"""Idea Forge must route by configuration, not by what a model is called.

call_idea_model used to send anything named gemini-* to Vertex regardless of the
config, so a Gemini reachable over an OpenAI-compatible endpoint was unusable even
when correctly configured. The model's name decided its protocol.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def test_user_alias_goes_through_the_shared_resolver(monkeypatch) -> None:
    forge = importlib.import_module("idea_forge.forge")
    seen = {}

    def fake_call_model(model_name, prompt, **kwargs):
        seen["model"] = model_name
        seen["kwargs"] = kwargs
        return "routed"

    monkeypatch.setattr(forge, "call_model", fake_call_model)

    assert forge.call_idea_model("my-ideator", "hi", max_tokens=2500, temperature=0.3) == "routed"
    assert seen["model"] == "my-ideator", "the alias must reach the resolver unchanged"
    assert seen["kwargs"] == {"max_tokens": 2500, "temperature": 0.3}


def test_every_model_takes_the_same_path(monkeypatch) -> None:
    forge = importlib.import_module("idea_forge.forge")
    calls = []
    monkeypatch.setattr(forge, "call_model", lambda name, prompt, **kw: calls.append(name) or "ok")

    for name in ("model-a", "model-b", "model-c", "model-d"):
        forge.call_idea_model(name, "hi")

    assert calls == ["model-a", "model-b", "model-c", "model-d"]


def test_forge_no_longer_imports_the_vertex_client() -> None:
    """Guards against the prefix branch coming back with the import."""
    source = (REPO_ROOT / "src" / "idea_forge" / "forge.py").read_text(encoding="utf-8")
    assert "gemini_vertex" not in source
    assert 'model_name.startswith("gemini-")' not in source


def test_idea_generation_scores_through_the_role_resolver(monkeypatch) -> None:
    """打分要经过 screener 角色派发，不能直连某一家。

    原来这条断言的是源码字面（连缩进一起），把打分抽成函数就红了——红的原因不是它要防的
    那件事。改成看真正发生了什么：拦住 call_model，断言收到的是走 resolver 的模型名。
    """
    assert "gemini_vertex" not in (REPO_ROOT / "idea_generation.py").read_text(encoding="utf-8")

    sys.path.insert(0, str(REPO_ROOT))
    import idea_generation

    seen = {}
    import llm_client
    monkeypatch.setattr(llm_client, "call_role",
                        lambda role, prompt, **kw: seen.update(role=role) or "[1, 2]")

    scores = idea_generation.score_titles([{"title": "a"}, {"title": "b"}])

    assert seen["role"] == "screener"
    assert scores == [1, 2]
