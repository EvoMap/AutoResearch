"""`data/verified/` 是生产命名空间，校验产物不能放进来。

三个消费者都按 `pipeline_v4_*.json` 这个 glob 无差别读 `data/verified/`：
`src/pipeline_v4.py` 的跨天去重黑名单、`src/generate_dashboard.py` 的看板、
`src/generate_idea_page.py` 的 idea 页（`idea_generation.py` 用同一组 glob）。glob 只看文件名，
分不出这一份是真采集还是一次校验演示。

#248 就是这么发生的：2026-07-19 为 claims ledger 补证据跑的两次校验运行落在了
`data/verified/pipeline_v4_validation_*.json`，于是看板凭空多出一天 20260719、70 条，
排在时间线第一行，而那天根本没有生产采集；那 70 个 url 还进了去重黑名单，生产链路以后
不会再研判它们。

正确的做法仓里已经有：`data/evidence/issue-25/` 同样是补证据归档的一次性产物，它放在
独立目录、带 README 和 SHA256，不匹配任何生产 glob。

所以这里收两道。一道看命名空间：`data/verified/` 里不许出现校验产物。一道看下游：看板
时间线上的每一天，都要能追到一份生产跑。上游放对了不等于下游干净，下游才是给人看的那端。
"""

from __future__ import annotations

import glob
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERIFIED = REPO / "data" / "verified"

# 文件名里出现这些词，说明这一份是校验、演示或回归用的产物，不该按生产采集读。
# 它们要么放 `data/validation/`，要么放 `data/evidence/<issue>/`，别放进生产目录。
NON_PRODUCTION_MARKERS = ("validation", "validate", "demo", "fixture", "sample", "example", "test")


def non_production_files() -> list[Path]:
    return sorted(
        p
        for p in VERIFIED.glob("*.json")
        if any(marker in p.stem.lower() for marker in NON_PRODUCTION_MARKERS)
    )


def test_data_verified_holds_only_production_runs():
    """`data/verified/` 是生产命名空间，一份校验产物落进来就会被三个 glob 全部吃掉。"""
    offenders = [p.name for p in non_production_files()]
    assert offenders == [], (
        f"{offenders} 按文件名看不是生产采集，却放在 data/verified/。"
        "生产侧的 pipeline_v4_*.json glob 会把它们当成真实采集日读走："
        "去重黑名单会拉黑这些 url，看板和 idea 页会把它们显示成一天的研究信号。"
        "校验产物放 data/validation/，某个 issue 的证据放 data/evidence/<issue>/，"
        "两处都不匹配生产 glob。"
    )


def _date_of(path: str) -> str | None:
    """按消费者的规则取日期：文件名里第一个 8 位数字段。

    这段规则在 generate_dashboard.py 和 generate_idea_page.py 里各写了一遍，这里照抄，
    因为要断言的正是它们的实际行为，不是它们本该有的行为。
    """
    for part in Path(path).stem.split("_"):
        if len(part) == 8 and part.isdigit():
            return part
    return None


def test_every_dashboard_day_traces_to_a_production_run():
    """看板显示的每一天都得有一份生产跑撑着，否则用户看到的是一天没发生过的采集。"""
    spec = importlib.util.spec_from_file_location("gd", REPO / "src" / "generate_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    shown = {date for date, candidates in module.collect_seeds_by_date() if candidates}
    production = {
        _date_of(f)
        for f in glob.glob(str(VERIFIED / "pipeline_v4_*.json"))
        if not any(marker in Path(f).stem.lower() for marker in NON_PRODUCTION_MARKERS)
    }

    invented = sorted(shown - production)
    assert invented == [], (
        f"看板上的 {invented} 追不到任何一份生产跑，这些天全部来自校验产物。"
        "对着看板读数的人会以为那天真的采集过。"
    )
