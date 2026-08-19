from __future__ import annotations

import httpx
import pytest

import api_retry
import llm_client
import providers


def test_exception_retries_share_one_attempt_budget():
    calls = []
    waits = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("temporary")
        return "ok"

    result = api_retry.retry_call(flaky, attempts=3, sleep=waits.append)

    assert result == "ok"
    assert len(calls) == 3
    assert waits == [1.0, 2.0]


def test_exhausted_exception_keeps_every_failure():
    with pytest.raises(api_retry.RetryError) as exc_info:
        api_retry.retry_call(
            lambda: (_ for _ in ()).throw(TimeoutError("temporary")),
            attempts=2,
            sleep=lambda _delay: None,
            operation_name="test endpoint",
        )

    assert len(exc_info.value.failures) == 2
    assert "第 1 次" in str(exc_info.value)
    assert "第 2 次" in str(exc_info.value)


def test_http_retries_only_transient_statuses():
    responses = iter([
        httpx.Response(503),
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(200),
    ])
    waits = []

    response = api_retry.retry_http(
        lambda: next(responses), attempts=3, sleep=waits.append)

    assert response.status_code == 200
    assert waits == [1.0, 7.0]


def test_http_does_not_retry_authentication_errors():
    calls = []

    response = api_retry.retry_http(
        lambda: calls.append(1) or httpx.Response(401),
        attempts=3,
        sleep=lambda _delay: pytest.fail("401 must not sleep"),
    )

    assert response.status_code == 401
    assert len(calls) == 1


def test_http_does_not_retry_non_transport_exceptions():
    calls = []

    with pytest.raises(ValueError, match="bad request setup"):
        api_retry.retry_http(
            lambda: calls.append(1) or (_ for _ in ()).throw(
                ValueError("bad request setup")),
            attempts=3,
            sleep=lambda _delay: pytest.fail("configuration errors must not sleep"),
        )

    assert len(calls) == 1


def test_provider_dispatch_defaults_to_three_attempts(monkeypatch):
    config = {
        "profiles": {
            "m": {
                "api": "openai_chat",
                "base_url": "https://example.invalid",
                "api_key": "secret",
                "model": "m",
            },
        },
    }
    calls = []
    monkeypatch.setattr(api_retry.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        providers,
        "send",
        lambda _wire, _dialect: calls.append(1)
        or providers.Reply(providers.REMOTE_ERROR, http_status=503),
    )

    reply = providers.dispatch(config, "m", "hello")

    assert len(calls) == 3
    assert len(reply.attempts) == 3


def test_provider_attempts_can_be_set_per_profile(monkeypatch):
    config = {
        "version": 2,
        "endpoints": {
            "gateway": {
                "dialect": "openai_chat",
                "base_url": "https://example.invalid",
                "credential_env": ["TEST_API_KEY"],
                "retry_attempts": 2,
                "retry_delay_seconds": 0,
            },
        },
        "models": {
            "m": {
                "routes": [{"endpoint": "gateway", "wire_name": "m"}],
            },
        },
    }
    calls = []
    monkeypatch.setattr(
        providers,
        "send",
        lambda _wire, _dialect: calls.append(1)
        or providers.Reply(providers.UNREACHABLE),
    )

    providers.dispatch(config, "m", "hello", env={"TEST_API_KEY": "secret"})

    assert len(calls) == 2


def test_model_call_can_override_attempts(monkeypatch):
    config = {
        "profiles": {
            "m": {
                "api": "openai_chat",
                "base_url": "https://example.invalid",
                "api_key": "secret",
                "model": "m",
                "retry_delay_seconds": 0,
            },
        },
    }
    calls = []
    monkeypatch.setattr(
        providers,
        "send",
        lambda _wire, _dialect: calls.append(1)
        or providers.Reply(providers.UNREACHABLE),
    )

    with pytest.raises(llm_client.ModelUnreachable):
        llm_client.call_model("m", "hello", _config=config, attempts=2)

    assert len(calls) == 2
