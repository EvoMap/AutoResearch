"""search_reddit_research turns a Reddit listing into normalised posts.

It had no coverage at all, which is how a lint cleanup broke it without turning
anything red: removing an unused assignment left the line below it indented
under `if not post: continue`, so reddit_url was never bound. Every valid post
then raised NameError, the surrounding `except Exception` swallowed it, and the
channel returned an empty list -- a silent loss of a whole source.

No network: the HTTP call is replaced with a canned listing.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "src" / "collectors"))


def listing(*posts):
    return {"data": {"children": [{"data": p} for p in posts]}}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


@pytest.fixture
def module(monkeypatch):
    # 原来这里还 patch 了 time.sleep：那是 pipeline_v4 的，采集器本身不 sleep。
    # 搬成模块之后属性不在了，patch 会直接 AttributeError。
    return importlib.import_module("reddit_collector")


def test_a_post_becomes_a_result(module, monkeypatch):
    """The case the bug broke: a valid post must survive the loop."""
    payload = listing({
        "title": "A paper worth reading",
        "permalink": "/r/MachineLearning/comments/abc/",
        "url": "https://arxiv.org/abs/2501.00001",
        "num_comments": 42,
        "score": 300,
        "created_utc": 1_760_000_000,
    })
    monkeypatch.setattr(module, "http_get", lambda *a, **kw: FakeResponse(payload))

    posts = module.search_reddit_research("MachineLearning", "paper")

    assert len(posts) == 1, "a valid post was dropped"
    assert posts[0]["title"] == "A paper worth reading"
    assert posts[0]["source"] == "reddit_machinelearning"


def test_an_external_link_wins_over_the_thread_url(module, monkeypatch):
    payload = listing({
        "title": "paper", "permalink": "/r/ml/comments/x/",
        "url": "https://arxiv.org/abs/2501.00002",
        "num_comments": 1, "score": 1, "created_utc": 1_760_000_000,
    })
    monkeypatch.setattr(module, "http_get", lambda *a, **kw: FakeResponse(payload))

    assert module.search_reddit_research("ml", "q")[0]["url"] == "https://arxiv.org/abs/2501.00002"


def test_a_self_post_falls_back_to_the_thread_url(module, monkeypatch):
    """A self post's url points back at reddit, so the permalink is what is left."""
    payload = listing({
        "title": "discussion", "permalink": "/r/ml/comments/y/",
        "url": "https://www.reddit.com/r/ml/comments/y/",
        "num_comments": 1, "score": 1, "created_utc": 1_760_000_000,
    })
    monkeypatch.setattr(module, "http_get", lambda *a, **kw: FakeResponse(payload))

    url = module.search_reddit_research("ml", "q")[0]["url"]
    assert url == "https://www.reddit.com/r/ml/comments/y/"


def test_a_lookalike_reddit_hostname_remains_an_external_link(module, monkeypatch):
    payload = listing({
        "title": "paper", "permalink": "/r/ml/comments/lookalike/",
        "url": "https://www.reddit.com.evil.example/paper",
    })
    monkeypatch.setattr(module, "http_get", lambda *a, **kw: FakeResponse(payload))

    post = module.search_reddit_research("ml", "q")[0]

    assert post["url"] == "https://www.reddit.com.evil.example/paper"


def test_a_non_http_external_url_falls_back_to_the_thread(module, monkeypatch):
    payload = listing({
        "title": "paper", "permalink": "/r/ml/comments/scheme/",
        "url": "javascript:alert(1)",
    })
    monkeypatch.setattr(module, "http_get", lambda *a, **kw: FakeResponse(payload))

    post = module.search_reddit_research("ml", "q")[0]

    assert post["url"] == "https://www.reddit.com/r/ml/comments/scheme/"


def test_a_malformed_permalink_cannot_change_the_reddit_hostname(module, monkeypatch):
    payload = listing({
        "title": "discussion", "permalink": "@evil.example/path",
        "url": "https://www.reddit.com/r/ml/comments/x/",
    })
    monkeypatch.setattr(module, "http_get", lambda *a, **kw: FakeResponse(payload))

    post = module.search_reddit_research("ml", "q")[0]

    assert post["reddit_url"] == "https://www.reddit.com"
    assert post["url"] == "https://www.reddit.com"


def test_an_empty_child_is_skipped_without_taking_the_batch_down(module, monkeypatch):
    """`if not post: continue` is the branch the broken indentation hid behind."""
    payload = listing(
        {},
        {"title": "real", "permalink": "/r/ml/comments/z/", "url": "https://arxiv.org/abs/3",
         "num_comments": 5, "score": 5, "created_utc": 1_760_000_000},
    )
    monkeypatch.setattr(module, "http_get", lambda *a, **kw: FakeResponse(payload))

    posts = module.search_reddit_research("ml", "q")
    assert [p["title"] for p in posts] == ["real"]


def test_a_non_200_returns_empty_rather_than_raising(module, monkeypatch):
    monkeypatch.setattr(module, "http_get", lambda *a, **kw: FakeResponse({}, status=429))
    assert module.search_reddit_research("ml", "q") == []
