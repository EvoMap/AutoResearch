from __future__ import annotations

import sys
import types

import pytest

import idea_generation


def install_replies(monkeypatch, replies):
    pending = iter(replies)
    module = types.ModuleType("llm_client")

    def call_role(*_args, **_kwargs):
        reply = next(pending)
        if isinstance(reply, Exception):
            raise reply
        return reply

    module.call_role = call_role
    monkeypatch.setitem(sys.modules, "llm_client", module)


def test_score_titles_retries_transient_failures(monkeypatch):
    install_replies(
        monkeypatch,
        [TimeoutError("timed out"), "not json", "[9, 7]"],
    )
    sleeps = []
    monkeypatch.setattr(idea_generation.time, "sleep", sleeps.append)

    scores = idea_generation.score_titles(
        [{"title": "first"}, {"title": "second"}],
        attempts=3,
        retry_delay=2,
    )

    assert scores == [9, 7]
    assert sleeps == [2, 4]


def test_score_titles_reports_count_mismatch_after_retries(monkeypatch):
    install_replies(monkeypatch, ["[9]", "[8]", "[7]"])
    monkeypatch.setattr(idea_generation.time, "sleep", lambda _delay: None)

    with pytest.raises(idea_generation.ScoreBatchError) as exc_info:
        idea_generation.score_titles(
            [{"title": "first"}, {"title": "second"}],
            attempts=3,
            retry_delay=0,
        )

    message = str(exc_info.value)
    assert message.count("分数数量不一致") == 3
    assert "第 1 次" in message and "第 3 次" in message
