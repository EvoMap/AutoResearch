"""公开采集渠道清单。

流水线遍历这份清单，页面和 ARCHITECTURE 的渠道表也从这里生成。
新增或删除渠道时只修改 CHANNELS，并由 `tests/test_channel_roster.py` 核对文档。
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SRC = Path(__file__).resolve().parent
COLLECTORS = SRC / "collectors"

# `normalize_post` 的第三类源类型，也是下游筛选读的那一档。页面上的分组用同一个轴：
# 页面原来另有一套分类（GitHub Trending 归「代码」、Paper Digest 归「媒体源」），和流水线
# 实际打的标签对不上，读者看到的分类于是不是系统在用的那个。
KINDS = {"community": "社区讨论", "academic": "学术", "media": "媒体"}


@dataclass(frozen=True)
class Window:
    """`collect_all_channels()` 收到的回溯参数，原样传给每一路。

    不按渠道改名：`time_filter` 有 Reddit 和 GitHub Trending 两路在用，
    `time_filter_days` 有 Hacker News 和研究者博客两路在用。
    """

    time_filter: str = "month"
    time_filter_days: int = 30
    arxiv_days: int = 7
    hf_days: int = 7


@dataclass(frozen=True)
class Channel:
    """一路采集渠道。

    `fetch` 拿到的是导入好的模块和这次的回溯窗口，返回条目列表；一路要分几段报数时返回
    `{段名: 条目}`（研究者博客那路分博客和顶会两段）。

    `required=True` 表示这一路失败就中断整条流水线。只有 Reddit 和 Hacker News 是这样，
    保持原有行为：其余九路各自被 try 包着，缺配置或超时的时候流水线照常跑完。
    """

    key: str
    label: str
    scope: str          # 覆盖范围，跟在名字后面：`arXiv（cs.AI/LG/CL/CV/MA）`
    about: str          # 典型内容，ARCHITECTURE 表格的最后一列
    kind: str           # KINDS 的键，normalize_post 的源类型
    unit: str           # 计数单位，采集时打在日志里
    module: str         # src/collectors/<module>.py
    fetch: Callable[[Any, Window], list | dict[str, list]]
    required: bool = False

    def title(self) -> str:
        return f"{self.label}（{self.scope}）" if self.scope else self.label


# GitHub Trending 的窗口只认这三个值，其余按月。
GITHUB_SINCE = {"month": "monthly", "week": "weekly", "year": "monthly"}

CHANNELS: tuple[Channel, ...] = (
    Channel(
        key="reddit", label="Reddit", scope="r/MachineLearning, r/LocalLLaMA, r/singularity",
        about="研究论文讨论帖", kind="community", unit="帖",
        module="reddit_collector", required=True,
        fetch=lambda m, w: m.get_reddit_hot_research(time_filter=w.time_filter),
    ),
    Channel(
        key="hackernews", label="Hacker News", scope="",
        about="AI 高评论量帖", kind="community", unit="帖",
        module="hackernews_collector", required=True,
        fetch=lambda m, w: m.get_hn_discussed(time_filter_days=w.time_filter_days),
    ),
    Channel(
        key="arxiv", label="arXiv", scope="cs.AI/LG/CL/CV/MA",
        about="最新论文，默认 7 天内", kind="academic", unit="篇",
        module="arxiv_collector",
        fetch=lambda m, w: m.collect_recent_papers(days_back=w.arxiv_days, max_per_category=20),
    ),
    Channel(
        key="hf_papers", label="HuggingFace Daily Papers", scope="",
        about="HF 精选论文，默认 7 天内", kind="academic", unit="篇",
        module="hf_papers_collector",
        fetch=lambda m, w: m.collect_daily_papers(days_back=w.hf_days),
    ),
    Channel(
        key="github_trending", label="GitHub Trending", scope="monthly",
        about="热门 AI 项目", kind="community", unit="个项目",
        module="github_trending_collector",
        fetch=lambda m, w: m.collect_trending(since=GITHUB_SINCE.get(w.time_filter, "monthly")),
    ),
    Channel(
        key="emergent_mind", label="Emergent Mind", scope="",
        about="论文社区热度排行", kind="academic", unit="篇",
        module="emergent_mind_collector",
        fetch=lambda m, w: m.collect_emergent_mind(),
    ),
    Channel(
        key="paper_digest", label="Paper Digest", scope="",
        about="每日精选摘要", kind="academic", unit="篇",
        module="paper_digest_collector",
        fetch=lambda m, w: m.collect_paper_digest(),
    ),
    Channel(
        key="rss", label="RSS", scope="量子位/雷锋网/MarkTechPost/VentureBeat",
        about="AI 新闻报道", kind="media", unit="篇",
        module="rss_collector",
        # collect_all_feeds 返回 (条目, 各源统计)，这里只要条目。
        fetch=lambda m, w: m.collect_all_feeds()[0],
    ),
    Channel(
        key="influential_voices", label="研究者博客 + 顶会 Spotlight", scope="",
        about="OpenAI/DeepMind/BAIR/Karpathy 博客", kind="academic", unit="篇",
        module="influential_voices",
        fetch=lambda m, w: {"博客": m.collect_research_blogs(max_days=w.time_filter_days),
                            "顶会": m.collect_conference_highlights()},
    ),
    Channel(
        key="openreview", label="OpenReview", scope="",
        about="ICLR/NeurIPS/ICML 最新投稿", kind="academic", unit="篇",
        module="openreview_collector",
        fetch=lambda m, w: m.collect_all_venues(),
    ),
    Channel(
        key="jina_chinese_media", label="Jina 中文媒体", scope="",
        about="机器之心/PaperWeekly 等", kind="media", unit="篇",
        module="jina_chinese_media",
        fetch=lambda m, w: m.collect_chinese_media(first_run=False),
    ),
)


def collection_channels() -> tuple[Channel, ...]:
    """返回公开发行版实际运行的渠道清单。"""
    return CHANNELS


def collect(channel: Channel, window: Window) -> dict[str, list]:
    """跑这一路，返回 `{段名: 条目}`。只有一段时段名是空串。"""
    if str(COLLECTORS) not in sys.path:
        sys.path.insert(0, str(COLLECTORS))
    items = channel.fetch(importlib.import_module(channel.module), window)
    return items if isinstance(items, dict) else {"": items}


def by_kind(channels: tuple[Channel, ...] | None = None) -> list[tuple[str, list[Channel]]]:
    """按源类型分组，给页面用。空的组不出现。"""
    roster = collection_channels() if channels is None else channels
    grouped = [(label, [c for c in roster if c.kind == kind]) for kind, label in KINDS.items()]
    return [(label, members) for label, members in grouped if members]


def markdown_table(channels: tuple[Channel, ...] | None = None) -> str:
    """ARCHITECTURE 里那张表。测试拿它和文件里的内容比对。"""
    roster = CHANNELS if channels is None else channels
    rows = [f"| {i} | {c.title()} | {c.kind} | {c.about} |" for i, c in enumerate(roster, 1)]
    return "\n".join(["| 序号 | 渠道 | 源类型 | 典型内容 |",
                      "|------|------|--------|---------|", *rows])
