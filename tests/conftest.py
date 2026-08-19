"""Fixtures shared by the Idea Forge tests.

consensus_check binds names out of idea_forge.b_library when it is imported, so a
test that redirects KNOWLEDGE_BASE_DIR has to redirect it on that same module
object. Loading a second copy of b_library changes nothing the gate reads, which
is why every one of these tests takes the modules from one place.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import llm_client as real_llm_client  # noqa: E402  路径插入之后才可导入

# A knowledge file carrying both sections the consensus prompt compares against.
COMPLETE_KNOWLEDGE = (
    "# 领域\n\n## 3. 常见误区 / 错误直觉\n- 误区一\n- 误区二\n\n"
    "## 5. 可行的创新切入点\n- 切入点一\n"
)


def load_module(name: str, rel: str):
    """Load a file as a module under a private name, without importing it."""
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def llm_client_double(**overrides):
    """A stand-in for llm_client carrying the whole surface the call sites import.

    One factory, not one stub per test file. Each file used to build its own and
    fill in whatever that file happened to need; adding `candidates_for` to the
    forge broke three of them at import time. A double that drifts from the thing
    it doubles is the same defect this release has been closing everywhere else —
    two copies of one list.

    The exception types are the real ones, so `except UnknownModel` behaves here
    exactly as it does in a run.
    """
    stub = types.ModuleType("llm_client")
    stub.UnknownModel = real_llm_client.UnknownModel
    stub.UnknownRole = real_llm_client.UnknownRole
    stub.call_pro = lambda *a, **k: "最终判定: 通过"
    stub.call_role = lambda role, prompt, **k: "最终判定: 通过"
    stub.call_role_with_model = lambda role, prompt, **k: ("最终判定: 通过", "stub-model")
    stub.call_model = lambda *a, **k: ""
    def configured(role):
        return ["stub-a", "stub-b", "stub-c"] if role == "ideator" else ["stub-model"]

    stub.candidates_for = configured
    stub.configured_role_models = configured
    stub.configured_distinct_role_models = configured
    stub.configured_request_params = lambda role=None, config=None: {"max_tokens": 8192}
    stub.configured_max_concurrency = lambda config=None: 1
    stub.load_config = lambda: {}
    for name, value in overrides.items():
        setattr(stub, name, value)
    return stub


@pytest.fixture
def forge(monkeypatch):
    """b_library and consensus_check, with the provider stubbed out.

    The stub goes in through monkeypatch, which removes it afterwards. sys.modules
    is process-global: an earlier version used setdefault and left an llm_client
    carrying only call_pro behind, and every later file then failed on
    `from llm_client import call_model`. Running that file alone stayed green,
    which is why the full suite is the one that counts.

    Returns bl / cc / llm. Assign to forge.llm.load_config or monkeypatch
    attributes on forge.cc to steer a case.
    """
    stub = llm_client_double()
    monkeypatch.setitem(sys.modules, "llm_client", stub)

    import idea_forge.b_library as bl

    cc = load_module("consensus_check_under_test", "src/idea_forge/consensus_check.py")
    monkeypatch.setattr(cc.time, "sleep", lambda *a: None)

    original = bl.KNOWLEDGE_BASE_DIR
    yield SimpleNamespace(bl=bl, cc=cc, llm=stub)
    bl.KNOWLEDGE_BASE_DIR = original
