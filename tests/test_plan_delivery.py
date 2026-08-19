"""一份计划书算不算交出来了，全仓只有一处说了算。

Step 3 生成失败时把失败本身写进记录的 `plan` 字段，读它的地方据此决定这条记录能不能用。
判据原来有四份各自手抄：forge 记数只认 `生成失败`，页面和 idea_provenance 各写了一遍两个
标记的集合。四份在仓库自带的数据上就已经分叉——`data/idea_forge/forge_20260509_1922.json`
的 4 条记录全是 `计划书生成失败`，记数说交付了 4 份计划书，页面一份都不渲染（#211）。

所以判据在 `src/plans.py`，这里两头都盯：它自己判得对，以及没人绕过它另写一份。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import plans  # noqa: E402  路径插入之后才可导入


def test_the_marker_the_producer_writes_is_not_a_plan() -> None:
    assert not plans.is_delivered(plans.FAILURE_MARKER)
    assert not plans.has_usable_plan({"plan": plans.FAILURE_MARKER})


def test_the_older_marker_counts_as_a_failure_too() -> None:
    """`计划书生成失败` 是早先版本写的，仓库里的数据还带着它。

    只认当前这一个，等于把 4 条失败记录当成 4 份计划书交出去。
    """
    assert not plans.is_delivered("计划书生成失败")
    assert not plans.has_usable_plan({"plan": "计划书生成失败"})


def test_a_plan_that_talks_about_failure_is_still_a_plan() -> None:
    """整值比较，不做子串测试：写着「若生成失败，重试一次」的是一份真计划书。"""
    assert plans.is_delivered("第一步：跑基线。若生成失败，重试一次，然后继续实验。")
    assert plans.has_usable_plan({"plan": "  一份真的计划书  "})


def test_nothing_written_is_not_a_plan() -> None:
    assert not plans.is_delivered(None)
    assert not plans.is_delivered("   ")
    assert not plans.has_usable_plan({})
    assert not plans.has_usable_plan(None)


def test_the_marker_around_whitespace_is_still_the_marker() -> None:
    assert not plans.is_delivered("  生成失败  ")


def test_the_readers_call_this_function_itself() -> None:
    """页面和 provenance 读的是同一个对象，不是各自等价的一份实现。"""
    import generate_idea_page
    import idea_provenance

    assert generate_idea_page.has_usable_plan is plans.has_usable_plan
    assert idea_provenance.is_delivered is plans.is_delivered


# 下面这条盯的是「有没有人自己写一份」，不是「判得对不对」——判得对不对由上面几条负责。

OWNER = "src/plans.py"

# 测试不在扫描范围里：用例把标记当输入数据喂进去是它该做的事，判它红只会教人把这条门关掉。
EXEMPT_PREFIXES = ("tests/",)


def tracked(*globs: str) -> list[str]:
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", *globs],
                            capture_output=True, text=True, check=True)
    return listed.stdout.split()


def spells_a_marker(source: str) -> str | None:
    """AST 而不是正则：注释和给人看的错误信息里出现这几个字是正常的。

    只看字符串字面量本身等不等于标记，所以 `# Rendering them would present 生成失败 as a
    plan` 这样的注释不算，而 `{"生成失败", "计划书生成失败"}` 算。
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and node.value in plans.FAILURE_MARKERS:
            return repr(node.value)
    return None


def test_no_other_file_spells_the_failure_markers() -> None:
    """再写一处就红在这里，而不是红在某份产出里两个都自称正确的数字上。"""
    offenders = {
        rel: marker
        for rel in tracked("*.py")
        if rel != OWNER and not rel.startswith(EXEMPT_PREFIXES)
        and (marker := spells_a_marker((REPO / rel).read_text(encoding="utf-8")))
    }

    assert not offenders, f"这些地方自己写了失败标记，应该问 {OWNER}：{offenders}"
