"""Tests for scripts/preflight.py.

Everything here runs in configuration-only mode, so no test sends a request or costs
anything. The parts that would talk to a provider are exercised through their pure
helpers instead.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "preflight.py"


def load_module():
    spec = importlib.util.spec_from_file_location("preflight", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pf = load_module()
# preflight 把 providers 加进了 sys.path 才 import 得到，所以要在它之后拿。
# 打桩要打在 providers 上：请求现在由它发，打在 preflight 上拦不住任何东西。
import providers  # noqa: E402


def write_config(tmp_path: Path, roles: dict, profiles: dict) -> Path:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"version": 1, "roles": roles, "profiles": profiles}))
    return path


def run(config: Path, env: dict[str, str] | None = None, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(config), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **(env or {})},
    )
    return result.returncode, result.stdout + result.stderr


OPENAI_PROFILE = {
    "api": "openai_chat",
    "base_url_env": "MY_BASE",
    "model": "m1",
    "api_key_env": "MY_KEY",
    "dialect": {"token_param": "max_tokens", "supports_temperature": True},
}


def test_all_credentials_present_passes(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {"screener": {"candidates": ["p1"]}},
        {"p1": OPENAI_PROFILE},
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 0
    assert "all required roles resolved" in out


def test_missing_credentials_names_the_variables(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {"screener": {"candidates": ["p1"]}},
        {"p1": OPENAI_PROFILE},
    )
    code, out = run(config)
    assert code == 1
    assert "MY_BASE" in out and "MY_KEY" in out
    assert "no credentials configured" in out


def test_falls_back_and_says_so(tmp_path: Path) -> None:
    """The first candidate is the recommendation; using a later one must be visible.

    Visible, but not as a failure. The first wording was "default unavailable, fell
    back", which reads as degradation and sends people off to configure a provider
    they do not need — half of the "too many APIs to set up" complaint is that.
    A recommendation means we tested that model in this role, not that the role
    requires it.
    """
    config = write_config(
        tmp_path,
        {"screener": {"candidates": ["default", "backup"]}},
        {
            "default": dict(OPENAI_PROFILE, api_key_env="ABSENT_KEY"),
            "backup": OPENAI_PROFILE,
        },
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 0
    assert "=> backup" in out
    assert "这次没配" in out and "换成别的照样跑" in out
    assert "AR_MODEL_SCREENER" in out, "要顺带告诉人哪个变量能指定"


def test_first_candidate_wins_when_available(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {"screener": {"candidates": ["default", "backup"]}},
        {"default": OPENAI_PROFILE, "backup": OPENAI_PROFILE},
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 0
    assert "=> default" in out
    assert "fell back" not in out


def test_all_candidates_used_role_reports_every_one(tmp_path: Path) -> None:
    """Idea Forge runs its models in parallel; they are not a fallback chain."""
    config = write_config(
        tmp_path,
        {"ideator": {"_all_candidates_used": True, "candidates": ["a", "b", "c"]}},
        {"a": OPENAI_PROFILE, "b": dict(OPENAI_PROFILE, model="m2"),
         "c": dict(OPENAI_PROFILE, model="m3")},
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 0
    assert "uses all of: a（" in out and "b（" in out and "c（" in out


def test_all_candidates_used_fails_when_the_panel_is_short(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {"ideator": {"_all_candidates_used": True, "candidates": ["a", "b", "c"]}},
        {"a": OPENAI_PROFILE, "b": dict(OPENAI_PROFILE, model="m2"),
         "c": dict(OPENAI_PROFILE, model="m3", api_key_env="ABSENT_KEY")},
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 1
    assert "only 2 of 3 required models available" in out


def test_an_old_two_model_minimum_cannot_weaken_the_runtime_contract(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {"ideator": {
            "_all_candidates_used": True,
            "_min_available": 2,
            "candidates": ["a", "b", "c"],
        }},
        {"a": OPENAI_PROFILE, "b": dict(OPENAI_PROFILE, model="m2"),
         "c": dict(OPENAI_PROFILE, model="m3", api_key_env="ABSENT_KEY")},
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 1
    assert "only 2 of 3 required models available" in out


def test_panel_meeting_its_minimum_passes(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {"ideator": {
            "_all_candidates_used": True,
            "candidates": ["a", "b", "c"],
        }},
        {
            "a": OPENAI_PROFILE,
            "b": dict(OPENAI_PROFILE, model="m2"),
            "c": dict(OPENAI_PROFILE, model="m3"),
        },
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 0
    assert "uses all of: a（" in out and "b（" in out


def test_role_requiring_an_api_rejects_the_wrong_protocol(tmp_path: Path) -> None:
    """Claude Code speaks Anthropic Messages; an OpenAI profile passes every
    credential check and then cannot run."""
    config = write_config(
        tmp_path,
        {"agent": {"_requires_api": "anthropic_messages", "candidates": ["p1"]}},
        {"p1": OPENAI_PROFILE},
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 1
    assert "requires api=anthropic_messages" in out


def test_unknown_role_filter_is_rejected(tmp_path: Path) -> None:
    """--role typo used to filter out everything and exit 0."""
    config = write_config(tmp_path, {"screener": {"candidates": ["p1"]}}, {"p1": OPENAI_PROFILE})
    code, out = run(config, {"MY_BASE": "b", "MY_KEY": "k"}, "--role", "does_not_exist")
    assert code != 0
    assert "unknown role" in out


def test_chat_completions_url_matches_the_mcp_reviewers() -> None:
    """ar-gemini-review-mcp 的 getChatCompletionsUrl() builds this URL, and only MCP profiles use it."""
    builder = providers.DIALECTS["openai_chat"]
    assert builder.url("https://h/openai/v1", "mcp") == "https://h/openai/v1/chat/completions"
    assert builder.url("https://h", "mcp") == "https://h/v1/chat/completions"
    assert builder.url("https://h/v1/chat/completions", "mcp") == "https://h/v1/chat/completions"
    assert builder.url("https://h/v1/", "mcp") == "https://h/v1/chat/completions"


def test_optional_role_does_not_block(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {"run_monitor": {"_optional": True, "candidates": ["p1"]}},
        {"p1": OPENAI_PROFILE},
    )
    code, _ = run(config)
    assert code == 0


def test_second_critic_is_optional_until_explicitly_requested(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {
            "critic": {"candidates": ["primary"]},
            "critic_secondary": {"_optional": True, "candidates": ["same_again"]},
        },
        {"primary": OPENAI_PROFILE, "same_again": dict(OPENAI_PROFILE)},
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})

    assert code == 0, out
    assert "treated as required" not in out
    assert "same model as critic" in out
    assert "no model independent of critic" in out

    code, out = run(
        config,
        {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"},
        "--role",
        "critic_secondary",
    )
    assert code == 1
    assert "normally optional; treated as required" in out
    assert "no model independent of critic" in out


def test_second_critic_skips_a_duplicate_and_uses_an_independent_fallback(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {
            # Reverse order deliberately: dependency ordering must not depend on JSON order.
            "critic_secondary": {"candidates": ["same_again", "independent"]},
            "critic": {"candidates": ["primary"]},
        },
        {
            "primary": OPENAI_PROFILE,
            "same_again": dict(OPENAI_PROFILE),
            "independent": dict(OPENAI_PROFILE, model="m2"),
        },
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})

    assert code == 0
    assert "same model as critic" in out
    assert "=> independent（m2）" in out


def test_unknown_profile_name_is_reported(tmp_path: Path) -> None:
    config = write_config(tmp_path, {"screener": {"candidates": ["nope"]}}, {})
    code, out = run(config)
    assert code == 1
    assert "no profile with this name" in out


def test_tools_without_live_is_rejected(tmp_path: Path) -> None:
    config = write_config(tmp_path, {"screener": {"candidates": ["p1"]}}, {"p1": OPENAI_PROFILE})
    code, out = run(config, None, "--tools")
    assert code != 0
    assert "--tools has no effect without --live" in out


def test_missing_config_explains_next_step(tmp_path: Path) -> None:
    code, out = run(tmp_path / "absent.json")
    assert code != 0
    assert "Next step" in out


def test_resolve_prefers_environment_indirection(monkeypatch) -> None:
    monkeypatch.setenv("SOME_VAR", "from-env")
    assert pf.resolve({"base_url_env": "SOME_VAR"}, "base_url") == "from-env"
    assert pf.resolve({"base_url": "literal"}, "base_url") == "literal"
    # An unset variable falls through to any literal value rather than returning None.
    monkeypatch.delenv("SOME_VAR", raising=False)
    assert pf.resolve({"base_url_env": "SOME_VAR", "base_url": "literal"}, "base_url") == "literal"


def test_missing_credentials_depends_on_api_shape(monkeypatch) -> None:
    monkeypatch.delenv("K", raising=False)
    monkeypatch.delenv("B", raising=False)
    openai = {"api": "openai_chat", "base_url_env": "B", "api_key_env": "K"}
    anthropic = {"api": "anthropic_messages", "base_url_env": "B", "auth_token_env": "K"}
    vertex = {"api": "vertex_generate", "service_account_env": "SA", "project_id_env": "PID"}
    assert providers.missing_credentials(openai) == ["B", "K"]
    assert providers.missing_credentials(anthropic) == ["B", "K"]
    assert providers.missing_credentials(vertex) == ["SA", "PID"]


class FakeResponse:
    def __init__(self, status_code: int, payload: dict, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        # 429 的 Retry-After 要被带出来，所以解析器会读 headers。
        self.headers = headers or {}

    def json(self):
        return self._payload


@pytest.mark.parametrize(
    "message, expected_hint",
    [
        (
            "Unsupported parameter: 'max_tokens' is not supported with this model. "
            "Use 'max_completion_tokens' instead.",
            "max_completion_tokens",
        ),
        ("Unsupported value: 'temperature' does not support 0.3", "supports_temperature"),
        ("This token is not allowed to use model: x", "没有该模型的授权"),
    ],
)
def test_http_errors_quote_the_fix(message: str, expected_hint: str) -> None:
    reply = providers.OpenAIChat().read(
        FakeResponse(400, {"error": {"message": message}}), 0.1)
    assert expected_hint in reply.detail + pf.describe_http_hint(reply, {})


def test_502_points_at_the_tool_replay() -> None:
    """A gateway that rejects a request shape only shows it from the second turn."""
    reply = providers.OpenAIChat().read(
        FakeResponse(502, {"error": {"message": "bad gateway"}}), 0.1)
    assert "--tools" in pf.describe_http_hint(reply, {})


def test_tool_ordering_failure_fails_the_candidate(monkeypatch) -> None:
    """A gateway that rejects [text, tool_use, text] cannot run a tool-using agent.

    Nothing in this repository rewrites that layout, so reporting the finding while
    still selecting the candidate would be a green run for a setup that breaks from
    the second turn.
    """
    profile = {
        "api": "anthropic_messages",
        "base_url": "https://h.example",
        "model": "m",
        "auth_token": "t",
    }
    monkeypatch.setattr(pf, "probe_anthropic_messages", lambda p: pf.ProbeResult(pf.OK, "returned 'OK'"))
    monkeypatch.setitem(pf.PROBES, "anthropic_messages", lambda p: pf.ProbeResult(pf.OK, "returned 'OK'"))
    monkeypatch.setattr(
        pf, "probe_tool_use_ordering",
        lambda p: pf.ProbeResult(pf.FAIL, "[text, tool_use, text] returned 502", quirks=["text_before_tool_use"]),
    )

    result = pf.evaluate("p", profile, live=True, tools=True)

    assert result.status == pf.FAIL
    assert "text_before_tool_use" in result.quirks


def test_probe_result_is_reused_across_roles(monkeypatch) -> None:
    """Roles share profiles; probing per role would bill the same endpoint twice."""
    calls = []

    def counting_probe(profile):
        calls.append(profile)
        return pf.ProbeResult(pf.OK, "returned 'OK'")

    monkeypatch.setitem(pf.PROBES, "openai_chat", counting_probe)
    profile = {"api": "openai_chat", "base_url": "https://h.example/v1", "model": "m", "api_key": "k"}
    cache: dict = {}

    first = pf.evaluate("shared", profile, live=True, tools=False, cache=cache)
    second = pf.evaluate("shared", profile, live=True, tools=False, cache=cache)

    assert len(calls) == 1, "the second role must reuse the first probe"
    assert first.status == pf.OK and second.status == pf.OK
    assert "reused" in second.detail


def test_url_style_follows_the_resolved_route(tmp_path: Path) -> None:
    gemini = "https://generativelanguage.googleapis.com/v1beta/openai/"
    builder = providers.DIALECTS["openai_chat"]
    assert builder.url(gemini, "append") == \
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert builder.url(gemini, "mcp") == \
        "https://generativelanguage.googleapis.com/v1beta/openai/v1/chat/completions"
    assert builder.url("https://h", "mcp") == "https://h/v1/chat/completions"
    assert builder.url("https://h/v1", "mcp") == "https://h/v1/chat/completions"
    assert builder.url("https://h", "append") == "https://h/chat/completions"


def test_default_gemini_profile_is_probed_where_the_consumer_calls(tmp_path: Path) -> None:
    from pathlib import Path as P
    config_path = P(__file__).resolve().parents[1] / "config" / "providers.example.json"
    example = json.loads(config_path.read_text(encoding="utf-8"))
    profile = providers.as_profiles(example)["gemini-3.1-flash-lite"]
    url = providers.DIALECTS["openai_chat"].url(
        profile["base_url"], profile.get("dialect", {}).get("url_style", "mcp"))
    assert url.endswith("/v1beta/openai/chat/completions"), url


def test_duplicate_candidates_count_once_and_fail_the_panel(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {"ideator": {"_all_candidates_used": True,
                      "candidates": ["a", "a_again", "third"]}},
        {"a": OPENAI_PROFILE, "a_again": dict(OPENAI_PROFILE),
         "third": dict(OPENAI_PROFILE, model="m3")},
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 1
    assert "not counted twice" in out
    assert "only 2 of 3 required models available" in out


def test_three_distinct_models_on_one_endpoint_count(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {"ideator": {"_all_candidates_used": True,
                      "candidates": ["a", "b", "c"]}},
        {"a": OPENAI_PROFILE, "b": dict(OPENAI_PROFILE, model="m2"),
         "c": dict(OPENAI_PROFILE, model="m3")},
    )
    code, _ = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 0


def test_dry_run_does_not_claim_reachability(tmp_path: Path) -> None:
    """A configuration-only run sends nothing, so it cannot report on endpoints."""
    config = write_config(
        tmp_path,
        {"agent": {"_requires_api": "anthropic_messages", "candidates": ["p1"]}},
        {"p1": OPENAI_PROFILE},
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 1
    assert "endpoint reachable" not in out
    assert "rejected before any request was sent" in out


def test_credential_fallback_variable_is_accepted(monkeypatch) -> None:
    """ar-external-critic-mcp.ts accepts GPT_CRITIC_* or OPENAI_*."""
    monkeypatch.delenv("GPT_CRITIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "from-openai")
    profile = {"api": "openai_chat", "api_key_env": "GPT_CRITIC_API_KEY",
               "api_key_env_fallback": "OPENAI_API_KEY", "base_url": "https://h/v1"}
    assert pf.resolve(profile, "api_key") == "from-openai"
    assert providers.missing_credentials(profile) == []


def test_missing_credentials_names_both_variables(monkeypatch) -> None:
    monkeypatch.delenv("GPT_CRITIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    profile = {"api": "openai_chat", "api_key_env": "GPT_CRITIC_API_KEY",
               "api_key_env_fallback": "OPENAI_API_KEY", "base_url": "https://h/v1"}
    assert providers.missing_credentials(profile) == ["GPT_CRITIC_API_KEY or OPENAI_API_KEY"]


def test_same_model_on_two_endpoints_is_one_voice(tmp_path: Path) -> None:
    """Serving one model from two hosts does not create a second opinion.

    Identity used to include base_url, so two endpoints running the same model
    satisfied a two-model panel.
    """
    config = write_config(
        tmp_path,
        {"ideator": {"_all_candidates_used": True,
                      "candidates": ["a", "b", "c"]}},
        {
            "a": dict(OPENAI_PROFILE, base_url="https://a.invalid/v1", base_url_env=None, model="same-model"),
            "b": dict(OPENAI_PROFILE, base_url="https://b.invalid/v1", base_url_env=None, model="same-model"),
            "c": dict(OPENAI_PROFILE, base_url="https://c.invalid/v1", base_url_env=None, model="same-model"),
        },
    )
    code, out = run(config, {"MY_KEY": "k"})
    assert code == 1, "one model behind multiple hosts is still one reviewer"
    assert "not counted twice" in out
    assert "only 1 of 3 required models available" in out


def test_distinct_model_opt_in_is_honoured(tmp_path: Path) -> None:
    """A deployment that genuinely differs can say so explicitly."""
    config = write_config(
        tmp_path,
        {"ideator": {"_all_candidates_used": True,
                      "candidates": ["a", "b", "c"]}},
        {
            "a": dict(OPENAI_PROFILE, base_url="https://a.invalid/v1", base_url_env=None,
                      model="same-model", distinct_model="same-model@a"),
            "b": dict(OPENAI_PROFILE, base_url="https://b.invalid/v1", base_url_env=None,
                       model="same-model", distinct_model="same-model@b"),
            "c": dict(OPENAI_PROFILE, base_url="https://c.invalid/v1", base_url_env=None,
                       model="same-model", distinct_model="same-model@c"),
        },
    )
    code, _ = run(config, {"MY_KEY": "k"})
    assert code == 0


def test_dry_run_never_claims_the_endpoint_answered(tmp_path: Path) -> None:
    """A protocol mismatch is decided before any request leaves the process."""
    config = write_config(
        tmp_path,
        {"agent": {"_requires_api": "anthropic_messages", "candidates": ["p1"]}},
        {"p1": OPENAI_PROFILE},
    )
    code, out = run(config, {"MY_BASE": "https://example.invalid/v1", "MY_KEY": "k"})
    assert code == 1
    assert "endpoint answered" not in out
    assert "rejected before any request was sent" in out
    assert "only credential gaps could be detected" not in out


def test_gpt_critic_accepts_the_full_consumer_chain(monkeypatch) -> None:
    """ar-external-critic-mcp.ts takes GPT_CRITIC_* || OPENAI_* || OPENAI_API_BASE."""
    from pathlib import Path as P
    config_path = P(__file__).resolve().parents[1] / "config" / "providers.example.json"
    example = json.loads(config_path.read_text(encoding="utf-8"))
    profile = providers.as_profiles(example)["gpt-critic-endpoint"]

    for name in ("GPT_CRITIC_BASE_URL", "OPENAI_BASE_URL", "GPT_CRITIC_API_KEY",
                 "GPT_CRITIC_MODEL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_BASE", "https://third.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "k")

    assert pf.resolve(profile, "base_url") == "https://third.invalid/v1"
    assert providers.missing_credentials(profile) == []
    # Falls through to the consumer's own default rather than a hardcoded gpt-5.5.
    assert pf.resolve(profile, "model") == "gpt-5-mini"


def test_gpt_critic_prefers_its_own_model_variable(monkeypatch) -> None:
    monkeypatch.setenv("GPT_CRITIC_MODEL", "chosen-model")
    from pathlib import Path as P
    config_path = P(__file__).resolve().parents[1] / "config" / "providers.example.json"
    example = json.loads(config_path.read_text(encoding="utf-8"))
    assert pf.resolve(providers.as_profiles(example)["gpt-critic-endpoint"],
                      "model") == "chosen-model"


def test_transport_failure_is_not_reported_as_a_refusal(tmp_path: Path) -> None:
    """A connection that never happened said the endpoint answered and refused.

    evaluate() stamped stage="request" over whatever the probe recorded, so a
    ConnectError landed in the bucket whose advice is "it is a config change, not a
    credential one". Nothing about the configuration was ever tested.
    """
    config = write_config(
        tmp_path,
        {"screener": {"_purpose": "cheap filter", "candidates": ["dead"]}},
        {"dead": {
            "api": "openai_chat",
            # Port 1 is reserved and nothing listens on it, so the connection is
            # refused before any byte of the request is written.
            "base_url": "http://127.0.0.1:1",
            "model": "m1",
            "api_key_env": "MY_KEY",
            "dialect": {"token_param": "max_tokens"},
        }},
    )

    code, out = run(config, {"MY_KEY": "k"}, "--live")
    assert code != 0
    assert "never reached the endpoint" in out
    assert "endpoint answered and refused" not in out
    assert "rejected before any request was sent" not in out


def test_a_probe_records_the_stage_it_reached() -> None:
    """The three stages must stay distinguishable at the source."""
    assert pf.transport_failure(OSError("boom"), 0.0).stage == "transport"
    assert pf.ProbeResult(pf.FAIL, "x").stage == "config"


def test_defaults_fill_gaps_in_an_older_local_config() -> None:
    """A role added to the tracked config must reach installs that copied it earlier.

    Reading the local copy wholesale meant `--role run_monitor` answered "unknown
    role" on every machine that had already made one, and nothing said why.
    """
    local = {"version": 1, "roles": {"screener": {"candidates": ["mine"]}},
             "profiles": {"mine": {"api": "openai_chat"}}}
    defaults = {"version": 1,
                "roles": {"screener": {"candidates": ["theirs"]},
                          "run_monitor": {"candidates": ["oai-small"]}},
                "profiles": {"theirs": {"api": "openai_chat"},
                             "oai-small": {"api": "openai_chat"}}}

    merged, added = pf.merge_with_defaults(local, defaults)

    assert "run_monitor" in merged["roles"], "the new role is missing"
    assert "oai-small" in merged["profiles"]
    assert merged["roles"]["screener"]["candidates"] == ["mine"], "local choices win"
    assert "role run_monitor" in added and "profile oai-small" in added
    assert "role screener" not in added, "only untouched entries are reported"


def test_merge_reports_what_it_added() -> None:
    """Filling a gap changes what gets checked, so it cannot happen silently."""
    _, added = pf.merge_with_defaults({"roles": {}, "profiles": {}},
                                      {"roles": {"r": {}}, "profiles": {"p": {}}})
    assert sorted(added) == ["profile p", "role r"]


def test_explicit_config_is_taken_exactly(tmp_path: Path) -> None:
    """--config names one file. Merging into it would make the flag mean less."""
    config = write_config(tmp_path, {"only": {"candidates": ["p1"]}}, {"p1": OPENAI_PROFILE})
    code, out = run(config, {"MY_BASE": "https://x", "MY_KEY": "k"})
    assert code == 0, out
    assert "run_monitor" not in out
    assert "taking them from" not in out


EXAMPLE = json.loads((Path(__file__).resolve().parents[1]
        / "config" / "providers.example.json").read_text(encoding="utf-8"))


def capture_request(monkeypatch, profile, body_text='data: {"candidates":[]}\n'):
    """Run the probe against a fake transport and return the URL and headers it built."""
    import httpx
    seen = {}

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, headers=None, json=None, **kwargs):
            seen["url"] = url
            seen["headers"] = headers or {}
            return httpx.Response(200, text=body_text)

    monkeypatch.setattr(providers.httpx, "Client", Client)
    result = pf.probe_gemini(profile)
    return seen, result


def test_anthropic_two_hundred_without_text_is_a_request_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint answered, so the summary must not say it never did."""
    import httpx

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def request(self, *args, **kwargs):
            return httpx.Response(200, json={"content": [{"type": "thinking"}]})

    monkeypatch.setattr(providers.httpx, "Client", Client)
    result = pf.probe_anthropic_messages({
        "api": "anthropic_messages", "base_url": "https://x",
        "auth_token": "k", "model": "m",
    })
    assert result.status == pf.FAIL
    assert result.stage == "request"


def test_cached_result_keeps_the_stage_it_was_decided_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """Roles share profiles, and the reused copy lost its stage.

    The first role was told the endpoint was never reached; the second, reading the
    same cached failure, was told it answered and refused.
    """
    cache = {"p1": pf.ProbeResult(pf.FAIL, "connection refused", 0.1, stage="transport")}
    result = pf.evaluate(
        "p1", {"api": "openai_chat", "base_url": "https://x", "api_key": "k", "model": "m"},
        live=True, tools=False, cache=cache,
    )
    assert result.stage == "transport"


@pytest.mark.parametrize("probe,profile", [
    (lambda: pf.probe_openai_chat,
     {"api": "openai_chat", "base_url": "https://x", "api_key": "k", "model": "m", "dialect": {}}),
    (lambda: pf.probe_anthropic_messages,
     {"api": "anthropic_messages", "base_url": "https://x", "auth_token": "k", "model": "m"}),
])
def test_two_hundred_with_a_non_json_body_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, probe, profile
) -> None:
    """A gateway answering 200 with an HTML error page ended the whole run.

    JSONDecodeError propagated out of the probe, so one unhealthy endpoint took
    down every remaining role instead of being one failed candidate.
    """
    import httpx

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def request(self, *args, **kwargs):
            return httpx.Response(200, text="<html>upstream error</html>")

    monkeypatch.setattr(providers.httpx, "Client", Client)
    result = probe()(profile)
    assert result.status == pf.FAIL
    assert result.stage == "request"
    assert "不是 JSON" in result.detail


def test_an_enabled_optional_role_can_still_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An explicitly requested optional role must be treated as required.

    With GPT_CRITIC_BASE_URL=1 and no Gemini credentials at all, preflight
    printed "no usable candidate" and then "all required roles resolved", exit 0.
    """
    for name in ("GEMINI_MODEL", "GEMINI_API_KEY", "GEMINI_BASE_URL", "ANTHROPIC_MODEL",
                 "GEMINI_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "GEMINI_VERTEX_SERVICE_ACCOUNT", "GOOGLE_APPLICATION_CREDENTIALS",
                 "GEMINI_VERTEX_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)

    example = Path(__file__).resolve().parents[1] / "config" / "providers.example.json"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "GPT_CRITIC_BASE_URL": "1"}
    code, out = run(example, env, "--role", "critic_secondary")
    assert code != 0, out
    assert "no usable candidate" in out


def test_naming_the_second_critic_checks_it_as_required(tmp_path: Path) -> None:
    """Asking about the required second critic must not return a false green."""
    example = Path(__file__).resolve().parents[1] / "config" / "providers.example.json"
    code, out = run(example, {"PATH": "/usr/bin:/bin:/usr/local/bin"}, "--role", "critic_secondary")
    assert code != 0, out
    assert "critic_secondary" in out


def test_optional_roles_are_left_out_of_default_failures() -> None:
    """Both run_monitor and the second critic are optional until activated."""
    example = Path(__file__).resolve().parents[1] / "config" / "providers.example.json"
    _, out = run(example, {"PATH": "/usr/bin:/bin:/usr/local/bin"})
    assert "unresolved" in out
    unresolved_line = [line for line in out.splitlines() if "required role(s) unresolved" in line][0]
    assert "run_monitor" not in unresolved_line
    assert "critic_secondary" not in unresolved_line




