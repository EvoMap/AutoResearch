from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import llm_client  # noqa: E402
import providers  # noqa: E402


def write_config(path: Path) -> None:
    path.write_text(json.dumps({
        "version": 2,
        "request_defaults": {"max_tokens": 8192},
        "endpoints": {
            "my-gateway": {
                "dialect": "openai_chat",
                "base_url_env": "MY_GATEWAY_URL",
                "credential_env": ["MY_GATEWAY_KEY"],
            },
        },
        "models": {
            "my-screening-alias": {
                "identity": "vendor/model-x",
                "routes": [{"endpoint": "my-gateway", "wire_name": "vendor/model-x"}],
            },
        },
        "roles": {"screener": {"models": ["my-screening-alias"]}},
    }), encoding="utf-8")


def test_explicit_empty_config_does_not_reload_another_authority(monkeypatch):
    monkeypatch.setattr(
        llm_client,
        "_provider_config",
        lambda: (_ for _ in ()).throw(AssertionError("must not reload config")),
    )

    assert llm_client._configured_candidates("screener", {}) == []


def test_bridge_self_test_uses_one_config_snapshot(monkeypatch):
    bridge = runpy.run_path(str(REPO / "scripts" / "call_role.py"))
    config = {"version": 2, "endpoints": {}, "models": {}, "roles": {}}
    seen = []
    monkeypatch.setattr(
        providers,
        "load_effective_config",
        lambda *_args, **_kwargs: (config, Path("providers.json"), []),
    )
    monkeypatch.setattr(
        llm_client,
        "configured_role_models",
        lambda role, supplied: seen.append((role, supplied)) or [],
    )

    assert bridge["ready_models"]("critic") == ([], [], [], "providers.json")
    assert seen == [("critic", config)]


def test_user_alias_controls_role_and_runtime(tmp_path, monkeypatch):
    config = tmp_path / "providers.json"
    write_config(config)
    monkeypatch.setenv("AUTORESEARCH_CONFIG", str(config))
    monkeypatch.setenv("MY_GATEWAY_URL", "https://example.invalid/v1")
    monkeypatch.setenv("MY_GATEWAY_KEY", "secret")

    seen = []
    monkeypatch.setattr(
        providers,
        "dispatch",
        lambda cfg, model, prompt, **kwargs: seen.append(
            (model, prompt, kwargs["max_tokens"]))
        or providers.Reply(providers.DELIVERED, text="ok"),
    )

    assert llm_client.configured_role_models("screener") == ["my-screening-alias"]
    assert llm_client.call_role("screener", "hello") == "ok"
    assert seen == [("my-screening-alias", "hello", 8192)]


def test_mcp_bridge_reads_the_same_config(tmp_path):
    config = tmp_path / "providers.json"
    write_config(config)
    process_env = os.environ.copy()
    process_env.update({
        "AUTORESEARCH_CONFIG": str(config),
        "MY_GATEWAY_URL": "https://example.invalid/v1",
        "MY_GATEWAY_KEY": "secret",
    })
    done = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "call_role.py"),
         "--role", "screener", "--self-test"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=process_env,
        timeout=30,
    )

    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report["configured_models"] == ["my-screening-alias"]
    assert report["ready_models"] == ["my-screening-alias"]
    assert report["ready_model_identities"] == ["vendor/model-x"]


def test_mcp_bridge_reports_the_model_that_replied(tmp_path):
    class Endpoint(BaseHTTPRequestHandler):
        seen_path = ""

        def log_message(self, *_args):
            pass

        def do_POST(self):
            type(self).seen_path = self.path
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({
                "choices": [{"message": {"content": "bridge-ok"}, "finish_reason": "stop"}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "providers.json"
        write_config(config)
        process_env = os.environ.copy()
        process_env.update({
            "AUTORESEARCH_CONFIG": str(config),
            "MY_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "MY_GATEWAY_KEY": "secret",
        })
        done = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "call_role.py"), "--role", "screener"],
            input=json.dumps({"prompt": "hello"}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=process_env,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert Endpoint.seen_path == "/v1/chat/completions"
    assert report["configured_models"] == ["my-screening-alias"]
    assert report["model"] == "my-screening-alias"
    assert report["model_identity"] == "vendor/model-x"
    assert report["text"] == "bridge-ok"


def test_role_result_reports_the_model_that_answered(tmp_path, monkeypatch):
    config = tmp_path / "providers.json"
    write_config(config)
    monkeypatch.setenv("AUTORESEARCH_CONFIG", str(config))
    monkeypatch.setattr(llm_client, "call_model", lambda model, prompt, **kwargs: "answer")

    result = llm_client.call_role_result("screener", "hello")

    assert result.model == "my-screening-alias"
    assert result.model_identity == "vendor/model-x"
    assert result.text == "answer"


def test_excluding_an_identity_skips_all_of_its_aliases(tmp_path, monkeypatch):
    config = tmp_path / "providers.json"
    body = {
        "version": 2,
        "endpoints": {
            "gateway": {
                "dialect": "openai_chat",
                "base_url": "https://example.invalid/v1",
                "credential_env": ["KEY"],
            },
        },
        "models": {
            "alias-a": {
                "identity": "shared-model",
                "routes": [{"endpoint": "gateway", "wire_name": "deployment-a"}],
            },
            "alias-b": {
                "identity": "shared-model",
                "routes": [{"endpoint": "gateway", "wire_name": "deployment-b"}],
            },
            "independent": {
                "identity": "other-model",
                "routes": [{"endpoint": "gateway", "wire_name": "deployment-c"}],
            },
        },
        "roles": {"critic_secondary": {"models": ["alias-a", "alias-b", "independent"]}},
    }
    config.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setenv("AUTORESEARCH_CONFIG", str(config))
    called = []
    monkeypatch.setattr(
        llm_client,
        "call_model",
        lambda model, prompt, **kwargs: called.append(model) or "answer",
    )

    result = llm_client.call_role_result(
        "critic_secondary", "hello", exclude_identities={"shared-model"})

    assert called == ["independent"]
    assert result.model == "independent"
    assert result.model_identity == "other-model"
