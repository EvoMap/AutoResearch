"""一份计划书算不算交出来了，只有这一份判据。

Step 3 给每条通过验证的 idea 生成计划书，生成失败时把失败本身写进 `plan` 字段。下游据此
分「有计划书」和「没有」：产出里的计划书数、页面渲不渲染这张卡、能不能拿它导出成一个可执行
项目，问的都是这一件事。

判据原来有四份手抄，其中一份认得的标记比别人少一个，在仓库自带的数据上就已经分叉：
`data/idea_forge/forge_20260509_1922.json` 的 4 条记录全是 `计划书生成失败`，产出里记着
交付了 4 份计划书，页面一份都不渲染（#211）。

纯函数，不读文件不发请求，产者和读者都从这里读。
"""

from __future__ import annotations

# Step 3 生成失败时写进 `plan` 的值。
FAILURE_MARKER = "生成失败"

# 认得出的失败标记。`计划书生成失败` 是早先版本写的，数据里还带着它，读者少认一个就会把
# 失败当成交付。
FAILURE_MARKERS = frozenset({FAILURE_MARKER, "计划书生成失败"})


def is_delivered(plan_text: object) -> bool:
    """这段文本是不是一份真的计划书。

    整值比较，不做子串测试：写着「若生成失败，重试一次」的是一份真计划书。
    """
    text = str(plan_text or "").strip()
    return bool(text) and text not in FAILURE_MARKERS


def has_usable_plan(record: object) -> bool:
    """Step 3 的一条记录里有没有可用的计划书。"""
    plan_text = record.get("plan") if isinstance(record, dict) else None
    return is_delivered(plan_text)
