"""一个角色用哪个模型，由角色决定，不由它碰巧落在哪个 provider 上决定。

调用点说的是用途（初筛、研判、构思、计划书），模型是数据。配置里给的每个模型都是
**推荐**——我们实测过它在这个角色上能用——而不是要求。换成别的模型是正常操作，不是降级。

换法是一个角色一个环境变量：

    AR_MODEL_SCREENER=<任何模型名>
    AR_MODEL_JUDGE=<任何模型名>
    AR_MODEL_IDEATOR=<模型一>,<模型二>,<模型三>
    AR_MODEL_PLANNER=<任何模型名>
    AR_MODEL_FRESHNESS_REFRESHER=<任何模型名>

原来的旋钮是按 profile 的：`judge` / `planner` / `freshness_refresher` / `ideator` 共用
一个 `OPENAI_MID_MODEL`，改一个动四个；`agent` 的旋钮叫 `ANTHROPIC_MODEL`，名字和角色
毫无关系；多数候选根本没有旋钮，换模型得去编辑 JSON。

preflight 和运行时读的是同一个函数。只让 preflight 认这个覆盖，就会出现「报告说用 A、
实际跑 B」——这个仓库清了一整轮的正是那种分叉。
"""

from __future__ import annotations

import os

# 普通角色的多个模型是回退链；只有面板角色会同时消费多个独立模型。
PANEL_ROLES = frozenset({"ideator"})

# 这些是运行时合同，不只是一份示例配置。把最低值留在代码里，旧的 local JSON 即使还写着
# “建议两个”或把字段整个漏掉，也不能让需要独立意见的环节悄悄降级。
MIN_INDEPENDENT_MODELS = {"ideator": 3}

# 第二路 critic 是另一个角色，因为两路各自需要独立的回退链。它必须避开第一路实际使用的
# 模型；同一模型换别名、换 endpoint 仍然只算一个意见。
INDEPENDENT_OF = {"critic_secondary": "critic"}


def env_name(role: str) -> str:
    return f"AR_MODEL_{role.upper()}"


def minimum_available(role: str, config: dict | None = None) -> int:
    """Return the hard number of independent models required by a role."""
    declared = (config or {}).get("_min_available", 1)
    try:
        declared = max(1, int(declared))
    except (TypeError, ValueError):
        declared = 1
    return max(declared, MIN_INDEPENDENT_MODELS.get(role, 1))


def recommended_available(role: str, config: dict | None = None) -> int:
    """Return a display recommendation without weakening the hard minimum."""
    minimum = minimum_available(role, config)
    recommended = (config or {}).get("_recommended_min_available", minimum)
    try:
        return max(minimum, int(recommended))
    except (TypeError, ValueError):
        return minimum


def independent_of(role: str, config: dict | None = None) -> str | None:
    """Return the role whose selected model this role must not reuse."""
    return str((config or {}).get("_independent_of") or INDEPENDENT_OF.get(role) or "").strip() or None


def optional_by_default(role: str, config: dict | None = None) -> bool:
    """Whether an inactive role may be absent without violating a workflow contract."""
    return bool((config or {}).get("_optional", False))


def override_for(role: str, env=None) -> list[str] | None:
    """这个角色被显式指定成了哪些模型，没指定就是 None。

    面板角色接受逗号分隔的多个名字；其余角色取第一个，多写的忽略而不是报错——写
    `AR_MODEL_JUDGE=a,b` 的人想表达的是 a，为此让整条管线起不来不值得。
    """
    raw = (os.environ if env is None else env).get(env_name(role))
    if raw is None:
        return None
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        return None
    return names if role in PANEL_ROLES else names[:1]


def models_for(role: str, recommended, env=None) -> list[str]:
    """这个角色这一次实际要用的模型名。

    recommended 可以是一个名字或一组名字，来自配置。覆盖存在时整个替换掉它，不做合并：
    「我要用这些」比「在推荐之上再加这些」更常见，也更容易预期。
    """
    default = [recommended] if isinstance(recommended, str) else list(recommended or [])
    return override_for(role, env=env) or default


def describe(role: str, recommended, env=None) -> str:
    """给报告用的一行：推荐什么、这次用什么、怎么换。"""
    default = [recommended] if isinstance(recommended, str) else list(recommended or [])
    actual = models_for(role, recommended, env=env)
    if actual == default:
        return f"推荐 {', '.join(default) or '(未声明)'}"
    return f"推荐 {', '.join(default) or '(未声明)'}，本次用 {', '.join(actual)}（{env_name(role)}）"
