"""ruff 的 per-file-ignores 只留有正当理由的那条。

`per-file-ignores` 是整文件生效：写一条 `"foo.py" = ["E501"]`，这个文件里**新增**的长行
也一起放行。八个文件曾经这么挂着，读起来像 grandfather list，实际是八个文件没有 lint（#31）。

现在只剩 `src/collectors/__init__.py` 的 F401，那条是真的：它 import 各采集器入口供
pipeline 取用，本来就不在本文件里使用。

新增豁免不是不行，但要在这里显式加一行，附上为什么。这一步是故意的摩擦——顺手加一条
豁免和顺手改一行配置的成本，不该是一样的。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

ALLOWED = {
    # 采集器模块把入口 import 进来供 pipeline 取用，本文件内不使用。
    "src/collectors/__init__.py": {"F401"},
    # 引擎与 demo 从 fork 树整体迁来（Phase C），风格历史欠账集中在长行与单字母
    # 变量；随下一次实质修改就地清掉，清掉时删除对应豁免。
    "ar-runtime/scripts/ar-workflow-engine.py": {"E501"},
    "ar-runtime/scripts/tests/demo_selforg_pool.py": {"E741"},
}


def declared_exemptions() -> dict[str, set[str]]:
    body = (REPO / "ruff.toml").read_text(encoding="utf-8")
    section = body[body.index("[lint.per-file-ignores]"):]
    found = {}
    for path, rules in re.findall(r'^"([^"]+)"\s*=\s*\[([^\]]*)\]', section, re.M):
        found[path] = set(re.findall(r'"([^"]+)"', rules))
    return found


def test_no_exemption_appears_without_being_recorded_here():
    declared = declared_exemptions()

    extra = {p: r for p, r in declared.items() if ALLOWED.get(p, set()) != r}
    assert not extra, (
        "ruff.toml 新增或改动了整文件豁免。整文件豁免会连同这个文件里*新增*的问题一起放行，"
        f"所以要在这个测试里显式记一笔并写清理由：{extra}")


def test_a_recorded_exemption_that_is_no_longer_needed_shows_up():
    """清单只准变短。留一条已经不需要的，它就慢慢变成没人维护的免罪符。"""
    declared = declared_exemptions()

    stale = {p for p in ALLOWED if p not in declared}
    assert not stale, f"这些豁免已经从 ruff.toml 删掉了，把它们从 ALLOWED 里也删掉：{stale}"
