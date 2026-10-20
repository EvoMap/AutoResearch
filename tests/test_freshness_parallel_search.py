"""Exercise the freshness dispatcher through real MCP SDK request/response handling."""

import asyncio
import json
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import llm_client  # noqa: E402
from idea_forge import freshness, parallel_search  # noqa: E402

REAL_CLIENT = httpx.AsyncClient
SOURCE_URL = "https://example.org/releases/model-next?version=2"
EVIDENCE = {"results": [{
    "title": "Model Next release", "url": SOURCE_URL,
    "excerpts": ["Model Next replaces the old model and supports the current benchmark."],
}]}


def idea(text="LLaVA-1.6 and Vicuna"):
    return {"idea_text": text}


@pytest.fixture
def workflow(monkeypatch, tmp_path):
    config = json.loads((Path(__file__).resolve().parents[1]
                         / "config/providers.example.json").read_text())
    config_path = tmp_path / "providers.json"

    def configure(provider="parallel", timeout=3, transport="direct"):
        config["freshness_search"] = {"provider": provider, "timeout_seconds": timeout,
                                      "proxy_contract": transport}
        config_path.write_text(json.dumps(config))
        monkeypatch.setenv("AUTORESEARCH_CONFIG", str(config_path))

    configure()
    prompts = []

    def model(role, prompt):
        assert role == "freshness_refresher"
        prompts.append(prompt)
        return "Evidence-based updated experiment plan. " * 5, "configured-refresher"

    monkeypatch.setattr(freshness, "call_role_with_model", model)
    monkeypatch.setattr(freshness, "build_b_library_reference", lambda _: "library-evidence")
    monkeypatch.setattr(freshness.time, "sleep", lambda _: None)
    return configure, prompts


@pytest.fixture
def mcp_wire(monkeypatch):
    records = []
    state = {"mode": "success", "payload": EVIDENCE, "cancelled": False, "clients": []}
    barrier = threading.Barrier(2)

    async def handle(request):
        assert str(request.url) == parallel_search.MCP_URL
        assert request.headers["user-agent"] == parallel_search.USER_AGENT
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        message = json.loads(request.content)
        records.append(message)
        method = message["method"]
        if "id" not in message:
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"],
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fixture-search", "version": "1"}}
        elif method == "tools/list":
            tools = [{"name": "web_search", "inputSchema": {
                "type": "object", "properties": {
                    "objective": {"type": "string"},
                    "search_queries": {"type": "array", "items": {"type": "string"}},
                    "session_id": {"type": "string"},
                }, "required": ["objective", "search_queries"],
            }}]
            result = {"tools": [] if state["mode"] == "missing_tool" else tools}
        elif method == "tools/call":
            assert message["params"]["name"] == "web_search"
            arguments = message["params"]["arguments"]
            assert arguments["search_queries"]
            assert uuid.UUID(arguments["session_id"])
            if state["mode"] == "interleaved":
                barrier.wait(timeout=5)
            if state["mode"] == "timeout":
                try:
                    await asyncio.sleep(10)
                finally:
                    state["cancelled"] = True
            if state["mode"] == "http_error":
                return httpx.Response(503)
            result = {"content": [{"type": "text", "text": json.dumps(state["payload"])}],
                      "isError": state["mode"] == "tool_error"}
            if state["mode"] == "quota_once":
                state["mode"] = "success"
                result = {"content": [{"type": "text", "text": "Anonymous quota exceeded"}],
                          "isError": True}
            if state["mode"] != "text_only":
                result["structuredContent"] = state["payload"]
        else:
            pytest.fail(f"unexpected MCP method {method}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        state["clients"].append(dict(kwargs))
        kwargs.pop("proxy", None)  # Observe the routing decision; keep the fixture offline.
        return REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(parallel_search.httpx, "AsyncClient", client)
    # Deliberately ambient: the anonymous provider must never consume these.
    monkeypatch.setenv("PARALLEL_API_KEY", "ambient-test-key")
    monkeypatch.setenv("HTTP_PROXY", "http://invalid.invalid:9")
    return records, state


def calls(records):
    return [r["params"]["arguments"] for r in records if r["method"] == "tools/call"]


@pytest.mark.parametrize("mode", ["success", "text_only"])
def test_saved_provider_dispatches_mcp_evidence_into_model(workflow, mcp_wire, mode):
    _, prompts = workflow
    records, state = mcp_wire
    state["mode"] = mode
    item = idea()
    assert freshness.step2_5_freshness_refresh([item]) == [item]
    assert SOURCE_URL in prompts[0]
    assert "Model Next replaces" in prompts[0]
    assert "library-evidence" in prompts[0]
    assert item["freshness_refresh"]["search_provider"] == "parallel"
    assert item["freshness_refresh"]["search_evidence_used"] is True
    assert item["freshness_refresh"]["arxiv_used"] is False
    assert item["freshness_refresh"]["refresher"] == "configured-refresher"
    assert {r["method"] for r in records} >= {"initialize", "tools/list", "tools/call"}
    assert len(calls(records)) == 2
    assert len({c["session_id"] for c in calls(records)}) == 1


def test_fresh_default_is_arxiv_and_explicit_override_wins(workflow, mcp_wire, monkeypatch):
    configure, prompts = workflow
    records, _ = mcp_wire
    configure("arxiv")
    arxiv_calls = []
    monkeypatch.setattr(freshness, "build_arxiv_reference",
                        lambda *a, **k: arxiv_calls.append(a) or "paper-evidence")
    item = idea()
    assert freshness.refresh_idea_freshness(item)
    assert not records
    assert arxiv_calls
    assert "paper-evidence" in prompts[0]
    assert item["freshness_refresh"]["arxiv_used"] is True
    freshness.refresh_idea_freshness(idea(), search_provider="parallel")
    assert calls(records)
    assert len(arxiv_calls) == 1
    configure("parallel")
    freshness.refresh_idea_freshness(idea(), search_provider="arxiv")
    assert len(arxiv_calls) == 2


def test_proxy_choice_uses_the_existing_contract_without_changing_auth(workflow, mcp_wire, monkeypatch):
    configure, _ = workflow
    _, state = mcp_wire
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.delenv("no_proxy", raising=False)
    freshness.refresh_idea_freshness(idea())
    assert "proxy" not in state["clients"][-1]
    configure(transport="standard-env")
    freshness.refresh_idea_freshness(idea())
    assert state["clients"][-1]["proxy"] == "http://proxy.example:8080"
    state["mode"] = "http_error"
    before = len(state["clients"])
    freshness.refresh_idea_freshness(idea())
    assert len(state["clients"]) == before + 1
    assert state["clients"][-1]["proxy"] == "http://proxy.example:8080"
    # A failed request never creates another client to bypass the explicit proxy.


@pytest.mark.parametrize("legacy_disable,override", [(True, "parallel"), (False, None),
                                                   (False, "none")])
def test_disabled_search_never_connects(workflow, mcp_wire, legacy_disable, override):
    configure, prompts = workflow
    records, _ = mcp_wire
    configure("none")
    item = idea()
    freshness.refresh_idea_freshness(item, enable_arxiv=not legacy_disable,
                                   search_provider=override)
    assert not records
    assert SOURCE_URL not in prompts[0]
    assert item["freshness_refresh"]["search_provider"] == "none"


def test_effective_local_configuration_and_missing_setting_keep_default(workflow, monkeypatch, tmp_path):
    _, prompts = workflow
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    tracked = {"freshness_search": {"provider": "arxiv", "timeout_seconds": 30}}
    (root / "config/providers.example.json").write_text(json.dumps(tracked))
    local = root / "config/providers.local.json"
    local.write_text(json.dumps({"freshness_search": {"provider": "none"}}))
    monkeypatch.delenv("AUTORESEARCH_CONFIG")
    monkeypatch.setattr(llm_client, "REPO_ROOT", root)
    freshness.refresh_idea_freshness(idea())
    assert SOURCE_URL not in prompts[0]
    local.write_text("{}")
    monkeypatch.setattr(freshness, "build_arxiv_reference", lambda *a, **k: "default-papers")
    freshness.refresh_idea_freshness(idea())
    assert "default-papers" in prompts[-1]


@pytest.mark.parametrize("mode", ["http_error", "tool_error", "missing_tool", "timeout"])
def test_failure_preserves_knowledge_evidence_without_provider_fallback(
        workflow, mcp_wire, monkeypatch, mode):
    configure, prompts = workflow
    records, state = mcp_wire
    configure(timeout=1 if mode == "timeout" else 3)
    state["mode"] = mode
    monkeypatch.setattr(freshness, "build_arxiv_reference",
                        lambda *a, **k: pytest.fail("unexpected provider fallback"))
    item = idea()
    assert freshness.refresh_idea_freshness(item)
    assert "library-evidence" in prompts[0]
    assert SOURCE_URL not in prompts[0]
    assert item["freshness_refresh"]["search_evidence_used"] is False
    if mode == "timeout":
        assert state["cancelled"]
    assert records


@pytest.mark.parametrize("payload", [{"oops": []}, {"results": [None, {
    "title": "unsafe", "url": "javascript:alert(1)", "excerpts": ["unsafe-evidence"],
}, {"title": "credentials", "url": "https://user:pass@example.org", "excerpts": ["unsafe-evidence"]}]}])
def test_malformed_and_unsafe_results_are_not_evidence(workflow, mcp_wire, payload):
    _, prompts = workflow
    _, state = mcp_wire
    state["payload"] = payload
    item = idea()
    freshness.refresh_idea_freshness(item)
    assert "unsafe-evidence" not in prompts[0]
    assert item["freshness_refresh"]["search_evidence_used"] is False


def test_new_batches_standalone_refresh_and_interleaved_runs_own_their_ids(workflow, mcp_wire):
    records, state = mcp_wire
    freshness.step2_5_freshness_refresh([idea(), idea()])
    first = calls(records)
    assert len(first) == 4
    assert len({c["session_id"] for c in first}) == 1
    records.clear()
    freshness.step2_5_freshness_refresh([idea()])
    assert calls(records)[0]["session_id"] != first[0]["session_id"]
    records.clear()
    state["mode"] = "interleaved"
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: freshness.refresh_idea_freshness(idea()), range(2)))
    interleaved = calls(records)
    ids = [c["session_id"] for c in interleaved]
    assert len(set(ids)) == 2
    assert all(ids.count(i) == 2 for i in set(ids))
    assert first[0]["session_id"] not in ids


def test_quota_does_not_rotate_the_batch_identity(workflow, mcp_wire):
    records, state = mcp_wire
    state["mode"] = "quota_once"
    items = [idea(), idea()]
    freshness.step2_5_freshness_refresh(items)
    assert items[0]["freshness_refresh"]["search_evidence_used"] is False
    assert items[1]["freshness_refresh"]["search_evidence_used"] is True
    assert len(calls(records)) == 3
    assert len({c["session_id"] for c in calls(records)}) == 1


def test_keyboard_interrupt_cancels_the_real_dispatcher_before_model_consumption(tmp_path):
    # A user interrupt targets the caller, rather than cancelling an SDK background
    # transport task (which can legitimately represent an ended server stream).
    code = r'''
import asyncio, os, signal, sys, threading
import httpx
from idea_forge import freshness, parallel_search
original = httpx.AsyncClient
async def handle(request):
    threading.Timer(0.05, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    try:
        await asyncio.sleep(10)
    finally:
        print("transport-cancelled", flush=True)
def client(**kwargs):
    return original(transport=httpx.MockTransport(handle), **kwargs)
parallel_search.httpx.AsyncClient = client
freshness.call_role_with_model = lambda *a, **k: sys.exit("unexpected model call")
try:
    freshness.refresh_idea_freshness({"idea_text": "Vicuna"}, search_provider="parallel")
except KeyboardInterrupt:
    print("caller-interrupted", flush=True)
else:
    sys.exit("interrupt swallowed")
'''
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10,
        cwd=Path(__file__).resolve().parents[1],
        env={"PATH": str(Path(sys.executable).parent),
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert "caller-interrupted" in result.stdout
    assert "transport-cancelled" in result.stdout


def test_freshness_script_bootstraps_its_local_imports(tmp_path):
    # Load the script as a file without adding src to sys.path, but leave its
    # live arXiv demonstration under __main__ unexecuted.
    script = Path(__file__).resolve().parents[1] / "src/idea_forge/freshness.py"
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import runpy, sys; runpy.run_path(sys.argv[1])", str(script)],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("provider,timeout", [("typo", 3), ("parallel", 0), ("parallel", True),
                                            ("parallel", 301), ("parallel", "30")])
def test_bad_settings_fail_without_network(workflow, mcp_wire, provider, timeout):
    configure, _ = workflow
    records, _ = mcp_wire
    configure(provider, timeout)
    with pytest.raises(ValueError, match="freshness_search"):
        freshness.refresh_idea_freshness(idea())
    assert not records
