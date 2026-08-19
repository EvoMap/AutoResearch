"""每个渠道都是 `src/collectors/` 下的一个模块，没有内联的例外。

Reddit 和 Hacker News 曾经直接写在 `src/pipeline_v4.py` 里，另外 10 个是模块。两种形态
并存的代价是具体的（#24）：

  加渠道没有唯一落点，仓里同时有两个先例；
  总入口的签名泄漏了具体渠道：`time_filter` 是 Reddit 的、`time_filter_days` 是 HN 的，
  而别的渠道用 `arxiv_days` / `hf_days`；
  内联的那部分没法单独测，要覆盖 `run_pipeline_v4` 只能把 `collect_all_channels` 整个换掉；
  HN 还长出过第二份实现，无人引用（#23 已删）。

这道门盯的是「形态一致」这件事本身，不是数量。数量会变，形态不该变。清单收进
`src/channels.py` 之后（#164），这里读那份表，不再数流水线里的编号注释。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import channels  # noqa: E402

PIPELINE = REPO / "src" / "pipeline_v4.py"
COLLECTORS = REPO / "src" / "collectors"


def test_every_channel_is_reached_through_a_collector_module():
    missing = [c.key for c in channels.collection_channels()
               if not (COLLECTORS / f"{c.module}.py").exists()]

    assert not missing, (
        f"这些渠道指向的采集模块不存在：{missing}。"
        "内联实现会让「加一个渠道」失去唯一落点。")


def test_the_pipeline_defines_no_collector_of_its_own():
    """采集函数不该定义在 pipeline 里，搬走之后别又搬回来。"""
    body = PIPELINE.read_text(encoding="utf-8")
    inline = re.findall(r"^def (get_\w*(?:hot|discussed|stories)\w*|search_\w+_research)\(", body, re.M)

    assert not inline, f"这些采集函数又长回 pipeline_v4 里了：{inline}"


def test_the_module_count_matches_the_channel_count():
    modules = {p.stem for p in COLLECTORS.glob("*.py")} - {"__init__"}
    wired = {c.module for c in channels.collection_channels()}

    assert modules == wired, (
        f"`src/collectors/` 和渠道清单对不上。多出来的文件没人调用："
        f"{sorted(modules - wired)}；清单里指向的模块不在目录里：{sorted(wired - modules)}")
