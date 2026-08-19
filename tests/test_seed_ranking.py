"""打分全挂时不能假装排过序。

`idea_generation.py` 用一个模型给 all_insightful 批量打分，取前 15 个当种子。调用失败时
`call_model` 返回 None，正则匹配不上 → `scores = []` → `zip(batch, [])` 一个都不配对 →
每条都落到 `setdefault("_rank_score", 5.0)`。于是排序是稳定排序、退化成输入顺序，日志
却打印「分数: 5.0~5.0」，读起来像真排过。

这是这个仓库里最安静的一次失败：整轮 LLM 不可用，产出一份看着正常的 top-15。

部分成功比全失败更需要说清楚：没打上分的条目拿 5.0 这个中间值，会把真的低分条目挤下去。
所以覆盖率要报出来，不是内部细节。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import seed_ranking  # noqa: E402

ITEMS = [{"title": f"item {i}"} for i in range(6)]


def score_all(batch):
    return [float(len(batch) - i) for i in range(len(batch))]


def score_nothing(batch):
    return []


def test_it_ranks_when_scoring_works():
    seeds, coverage = seed_ranking.rank(ITEMS, score_all, batch_size=3, top=4)

    assert [s["_rank_score"] for s in seeds] == [3.0, 3.0, 2.0, 2.0]
    assert coverage.scored == 6 and coverage.total == 6
    assert coverage.ranked is True


def test_a_total_scoring_failure_is_not_a_ranking():
    """全挂时不排序、不打分，并且说出来。"""
    seeds, coverage = seed_ranking.rank(ITEMS, score_nothing, batch_size=3, top=4)

    assert coverage.scored == 0
    assert coverage.ranked is False, "一个分都没拿到，不能声称排过序"
    assert [s["title"] for s in seeds] == [i["title"] for i in ITEMS[:4]], "应保持输入顺序"
    assert all("_rank_score" not in s for s in seeds), "没打上的分不该伪造成 5.0"
    assert len(coverage.failures) == 2
    assert all("未返回任何分数" in failure for failure in coverage.failures)


def test_partial_coverage_is_reported():
    """一半批次挂掉时，未打分的条目不能用一个中间值把真低分挤下去。"""
    calls = {"n": 0}

    def flaky(batch):
        calls["n"] += 1
        return score_all(batch) if calls["n"] == 1 else []

    seeds, coverage = seed_ranking.rank(ITEMS, flaky, batch_size=3, top=6)

    assert coverage.scored == 3 and coverage.total == 6
    assert coverage.ranked is True
    assert [s["title"] for s in seeds[:3]] == ["item 0", "item 1", "item 2"]
    assert all("_rank_score" not in s for s in seeds[3:]), "未打分的排在后面且不伪造分数"


def test_a_raising_scorer_records_the_exception():
    """端点异常仍表示这一批没有分，但具体原因必须保留下来。"""
    def boom(batch):
        raise RuntimeError("endpoint down")

    seeds, coverage = seed_ranking.rank(ITEMS, boom, batch_size=3, top=4)

    assert coverage.scored == 0 and coverage.ranked is False
    assert len(seeds) == 4
    assert len(coverage.failures) == 2
    assert all("RuntimeError: endpoint down" in failure for failure in coverage.failures)


def test_the_summary_line_says_what_happened():
    _, none = seed_ranking.rank(ITEMS, score_nothing, batch_size=3, top=4)
    assert "未排序" in none.describe() and "0/6" in none.describe()

    _, full = seed_ranking.rank(ITEMS, score_all, batch_size=3, top=4)
    assert "6/6" in full.describe()
