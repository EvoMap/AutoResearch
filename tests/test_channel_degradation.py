"""一路渠道挂掉，整条流水线停还是继续。

`collect_all_channels` 遍历 `src/channels.py` 的清单，失败半径由 `Channel.required` 一个布尔
值决定：Reddit 和 Hacker News 是这条流水线的主信号，它们空了整轮就没有意义，所以立刻停；
其余每一路缺配置、上游超时、模块没装都只该少一路信号（#187）。

两个方向改错都不会有任何提示：把非必需的那一路标成 required，一份过期的 cookie 就能让凌晨
那次跑批整条停掉；反过来把 Reddit 标成可选，主信号断了也会静默跑完一轮空批。

全部离线：清单换成构造出来的渠道，`module` 用 stdlib，`fetch` 不发请求。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "collectors"))

import channels  # noqa: E402

pipeline = importlib.import_module("pipeline_v4")


def fake(key, *, required=False, boom=False):
    """一路假渠道。`module` 取 stdlib 只为了让 `channels.collect` 的那次 import 有东西可导。"""
    return channels.Channel(
        key=key, label=key, scope="测试", about="测试", kind="community", unit="条",
        module="json", required=required,
        fetch=(lambda m, w: (_ for _ in ()).throw(RuntimeError(f"{key} 挂了"))) if boom
        else (lambda m, w: [{"title": key, "url": f"https://example.invalid/{key}"}]),
    )


def roster(monkeypatch, *members):
    monkeypatch.setattr(channels, "collection_channels", lambda: tuple(members))


def test_an_optional_channel_that_fails_costs_one_signal_not_the_run(monkeypatch, capsys):
    roster(monkeypatch, fake("healthy"), fake("broken", boom=True), fake("also-healthy"))

    posts = pipeline.collect_all_channels()

    assert sorted(p["title"] for p in posts) == ["also-healthy", "healthy"]
    assert "broken 挂了" in capsys.readouterr().out, "这一路失败连行日志都没有，出事时无从查起"


def test_a_required_channel_that_fails_stops_the_run(monkeypatch):
    roster(monkeypatch, fake("healthy"), fake("main-signal", required=True, boom=True))

    with pytest.raises(RuntimeError, match="main-signal 挂了"):
        pipeline.collect_all_channels()


def test_the_main_signals_are_the_only_ones_that_can_stop_the_run():
    """哪几路是主信号锁在这里：改这份名单要先想清楚上面两条的代价。"""
    required = sorted(c.key for c in channels.CHANNELS if c.required)

    assert required == ["hackernews", "reddit"]
