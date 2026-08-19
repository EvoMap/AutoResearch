"""渠道清单只有一处可写，其余三处从它推导。

清单原来是四份手抄：流水线里的 11 段 import 是唯一会被执行的，ARCHITECTURE 的表格和两个
页面生成脚本各抄一份。抄漏没有任何提示，于是三份里有两份漂了：看板漏报 Emergent Mind、
OpenReview 和 Jina 中文媒体，Idea 页面漏报 Jina 中文媒体，读者看到的采集面比系统实际跑的窄
（#164）。

现在流水线遍历 `src/channels.py`，两个页面从它渲染，表格由这里对着它断言。加一路渠道只改
那一份表；表格忘了跟上，这道门会红。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "collectors"))

import channels  # noqa: E402
import generate_dashboard as dashboard  # noqa: E402
import generate_idea_page as idea_page  # noqa: E402

ARCHITECTURE = REPO / "ARCHITECTURE.md"


def test_the_architecture_table_is_the_roster():
    table = channels.markdown_table()

    assert table in ARCHITECTURE.read_text(encoding="utf-8"), (
        "ARCHITECTURE 的采集信号表和 src/channels.py 对不上。表格是手写的，清单不是，"
        f"把下面这段贴回 ARCHITECTURE.md 的「### 采集信号」下面：\n\n{table}")


def test_both_pages_name_every_channel():
    surfaces = {
        "index.html 的信号源行": dashboard.render_sources(),
        "ideas.html 的采集渠道": idea_page.render_channel_list(),
    }

    for where, rendered in surfaces.items():
        missing = [c.title() for c in channels.collection_channels() if c.title() not in rendered]
        assert not missing, f"{where}漏报了：{missing}"


def test_every_channel_calls_its_collector_the_way_the_collector_is_written():
    """清单里的 `fetch` 对着真模块的签名跑一遍：函数名或参数名写错，这里就红。

    改采集器签名不会碰到 `channels.py`，两边是分开的文件；不核一次的话，错要等到凌晨那次
    真采集才暴露。`create_autospec` 按真签名建替身，不发任何请求。
    """
    window = channels.Window()

    for channel in channels.collection_channels():
        module = importlib.import_module(channel.module)
        try:
            channel.fetch(mock.create_autospec(module, spec_set=True), window)
        except (AttributeError, TypeError) as e:
            raise AssertionError(
                f"渠道 {channel.key} 的 fetch 和 {channel.module} 对不上：{e}") from e


def test_every_channel_lands_in_a_group_the_pages_render():
    """`kind` 打错字的渠道会从两个页面上消失，而流水线照跑，只是没人看得见。"""
    grouped = {c.key for _, members in channels.by_kind() for c in members}
    roster = {c.key for c in channels.collection_channels()}

    assert roster - grouped == set(), (
        f"这些渠道的 kind 不在 channels.KINDS 里，页面不会显示它们："
        f"{sorted(roster - grouped)}，KINDS={sorted(channels.KINDS)}")
