"""给候选打分排序，并且说清楚这一轮到底排没排成。

调用点原来内联在 `idea_generation.py` 里：批量打分失败时 `call_model` 返回 None，正则匹配不上
就得到空分数表，`zip(batch, [])` 一条都不配对，于是每条都落到 `setdefault(..., 5.0)`。
稳定排序把它们原样留在输入顺序上，日志却打印「分数: 5.0~5.0」——整轮 LLM 不可用，产出
一份看着正常的 top-15。

所以这里把两件事分开：排序是排序，「排没排成」是要报出来的事实。没拿到分的条目不伪造
分数，也不给一个会把真低分挤下去的中间值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

SCORE_KEY = "_rank_score"


@dataclass(frozen=True)
class Coverage:
    """这一轮有多少条真的拿到了分。"""

    scored: int
    total: int
    failures: tuple[str, ...] = ()

    @property
    def ranked(self) -> bool:
        """一个分都没拿到就不算排过序。"""
        return self.scored > 0

    def describe(self) -> str:
        if not self.ranked:
            return f"打分全部失败（{self.scored}/{self.total}），未排序"
        if self.scored < self.total:
            return f"部分打分成功（{self.scored}/{self.total}），未打分的排在后面"
        return f"打分完成（{self.scored}/{self.total}）"


def rank(
    items: Sequence[dict[str, Any]],
    score_batch: Callable[[list[dict[str, Any]]], Iterable[float]],
    *,
    batch_size: int = 20,
    top: int = 15,
) -> tuple[list[dict[str, Any]], Coverage]:
    """按分数取前 `top` 个，并回报覆盖率。

    `score_batch` 抛异常和返回空表都表示这一批没有分，但原因会进入
    `Coverage.failures`，让上层能记录端点异常、解析失败或空返回。

    没拿到分的条目排在所有拿到分的后面，且不写 `_rank_score`——写一个中间值会让它们
    压过真实的低分条目，而它们只是没被评价过，不是被评价为中等。

    分数写进副本，不改调用方的 dict。原来那版就地改 `it["_rank_score"]`，于是「有没有
    分」这个状态会跨调用留在对象上，第二次跑读到的是上一次的残留。
    """
    scored: list[dict[str, Any]] = []
    unscored: list[dict[str, Any]] = []
    failures: list[str] = []

    for start in range(0, len(items), batch_size):
        batch = list(items[start:start + batch_size])
        batch_number = start // batch_size + 1
        span = f"第 {start + 1}-{start + len(batch)} 条"
        raised = False
        try:
            scores = list(score_batch(batch))
        except Exception as exc:                      # noqa: BLE001
            raised = True
            failures.append(
                f"第 {batch_number} 批（{span}）: {type(exc).__name__}: {exc}")
            scores = []
        if not scores and not raised:
            failures.append(f"第 {batch_number} 批（{span}）: 未返回任何分数")
        for item, value in zip(batch, scores):
            try:
                scored.append({**item, SCORE_KEY: float(value)})
            except (TypeError, ValueError):
                unscored.append(item)
        # 分数比条目少时，尾巴上这些没配到分；比条目多时多出来的直接丢掉，宁可少排也不错位。
        unscored.extend(batch[len(scores):])

    scored.sort(key=lambda x: x[SCORE_KEY], reverse=True)
    ordered = scored + unscored
    return ordered[:top], Coverage(
        scored=len(scored), total=len(items), failures=tuple(failures))
