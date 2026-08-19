"""合并门跑的是不是它声称跑的那些检查，以及 CONTRIBUTING 说的是不是同一批。

`imports-resolve-from-a-clone` 是 ruleset 里的必需检查，按名字要求。名字对上、内容被掏空的
workflow 会照常报绿：把某一步删掉、把命令换成 `true`、或者只留下 job 名，PR 都能合。
`integration_id` 只保证这个名字来自 GitHub Actions，管不了它做了什么。

真正的堵法是 org 级 required workflows，需要 org admin，现在够不着（见
docs/repo-governance.md）。这个文件挡的是另一种、也是这个仓库更可能发生的一种：有人顺手简化
CI 时删掉一步，而门照样绿。

第二件事同源。CONTRIBUTING 的 `## Tests` 给贡献者一份命令清单，末尾曾写着「CI runs the same
checks」，而 CI 实际多跑八项。照文档做到本地全绿的人，被 CI 拦下的方式恰好是文档说不会发生
的那种（#231）。两份清单分开维护、只有一份有门，没门的那份就会一直旧下去。所以这里放三份
清单互相对账：CI 跑什么、要求贡献者跑什么、差集为什么可以不要求。

按命令断言，不按步骤名：步骤改名是正常的，命令消失不是。清单里每条是「足以认出这项检查」的
那一段，不是完整 argv。同一项检查在 CI 上指向 `git archive` 导出的树，在本地指向 checkout，
判据相同而参数不同。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CONTRIBUTING = REPO / "CONTRIBUTING.md"
CONTRIBUTOR_SECTION = "## Tests"

# ruleset 里 required_status_checks 的 context 就是这个 job 名。改了它，必需检查再也不会出现，
# 而 PR 会永远停在 BLOCKED——错误表现和「CI 还没跑完」一模一样。
REQUIRED_CHECK = "test"

# 合并门必须跑的。删掉哪条，就等于把那道门关了。
CI_MUST_RUN = {
    "ruff check": "Python lint",
    "python -m pytest tests/ ar-runtime/scripts/tests/": "单元测试，含 workflow engine 的协议测试",
    "grep -qE '[0-9]+ skipped'": "整模块被 importorskip 跳过时要红，见 #17 的教训",
    "scripts/check_model_references.py": "调用点提到的模型和 env 都在配置里声明过",
    "scripts/render_env.py --check": "tracked 那层 env 投影与配置一致",
    "scripts/check_release_tree.py": "发布树里没有被禁路径",
    "scripts/check_public_knowledge.py": "发布树里的知识文件都在审阅过的清单上",
    "scripts/secret_scan.py": "发布树里没有未审阅的 secret 形态",
    "bun run typecheck": "本仓自有 TS 零类型错误",
    "bunx biome check scripts/ar-*.ts": "本仓自有 TS 的 lint",
    "bun test scripts/ar-*.test.ts": "本仓自有 TS 的测试",
    "scripts/ar-preflight-mcp.sh": "缺凭据必须退 1、本地实调正控必须退 0，两个方向都查",
}

# CONTRIBUTING 的 `## Tests` 必须写出来的。少一条，照文档做的人就会在 CI 上撞见没听说过的门。
CONTRIBUTOR_MUST_RUN = {
    key: CI_MUST_RUN[key]
    for key in (
        "ruff check",
        "python -m pytest tests/ ar-runtime/scripts/tests/",
        "scripts/check_model_references.py",
        "scripts/render_env.py --check",
        "scripts/check_release_tree.py",
        "scripts/check_public_knowledge.py",
        "scripts/secret_scan.py",
        "bun run typecheck",
        "bunx biome check scripts/ar-*.ts",
        "bun test scripts/ar-*.test.ts",
    )
}

# CI 跑、但不要求贡献者本地跑的。值是这一节正文里必须出现的那个词：把差集写进文档，
# 「CI 跑的是超集」才是一句能被核对的话，而不是一句免责声明。
CI_ONLY = {
    # 判据是 pytest 的输出而不是一条命令，本地看得见 `N skipped`，不必再 tee 一份日志去 grep。
    "grep -qE '[0-9]+ skipped'": "skipped",
    # 要把一批环境变量清空再跑两遍，正着一遍反着一遍。接线本身由 tests/test_release_checks.py
    # 断言，那份测试贡献者已经在跑了。
    "scripts/ar-preflight-mcp.sh": "ar-preflight-mcp.sh",
}


@pytest.fixture(scope="module")
def workflow():
    """Read as text on purpose.

    Parsing it would need PyYAML, which is not a dependency of this project. An
    importorskip would drop this whole file, and a silently skipped guard guards
    nothing -- CI's own "no test was skipped" step exists because that already
    happened once.
    """
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def commands(workflow):
    """Everything the job would execute, with comments removed.

    A command that only survives inside a comment no longer runs, so the
    assertions below must not see it. Both shapes count: a whole comment line, and
    `run: # cmd`, which is a step that does nothing while still reading like one.
    An earlier version only handled the first, and commenting out the secret scan
    slipped past this file.
    """
    kept = []
    for line in workflow.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        kept.append(re.split(r"(?<=\s)#", line, maxsplit=1)[0])
    return "\n".join(kept)


@pytest.fixture(scope="module")
def contributor_section():
    """CONTRIBUTING 里 `## Tests` 那一节的正文，到下一个二级标题为止。"""
    text = CONTRIBUTING.read_text(encoding="utf-8")
    parts = text.split(f"\n{CONTRIBUTOR_SECTION}\n", 1)
    assert len(parts) == 2, f"CONTRIBUTING 里没有 {CONTRIBUTOR_SECTION!r} 这一节了，这道门失去了判据"
    return re.split(r"\n## ", parts[1], maxsplit=1)[0]


@pytest.fixture(scope="module")
def contributor_commands(contributor_section):
    """只取这一节里的 bash 代码块。

    整篇搜的话，文档别处顺口提一句命令名就能让断言通过，而贡献者照着 `## Tests` 抄的是代码块。
    门要读的是被执行的那份东西。
    """
    blocks = re.findall(r"```bash\n(.*?)```", contributor_section, flags=re.DOTALL)
    assert blocks, f"{CONTRIBUTOR_SECTION} 一节里没有 bash 代码块，贡献者没有可抄的东西"
    return "\n".join(blocks)


def test_the_required_check_has_a_job_to_report_it(workflow):
    """改 job 名，必需检查就再也不会出现，PR 会永远停在 BLOCKED。

    错误表现和「CI 还没跑完」一模一样：checks 面板里没有这个名字，看不出在等什么。
    """
    assert f"\n  {REQUIRED_CHECK}:" in workflow, (
        f"ruleset 按 {REQUIRED_CHECK!r} 要求必需检查，workflow 里没有同名 job。"
        f"改名之前先改 ruleset。"
    )


@pytest.mark.parametrize("command, why", CI_MUST_RUN.items(), ids=list(CI_MUST_RUN))
def test_the_gate_still_runs(commands, command, why):
    assert command in commands, f"合并门不再跑「{why}」：{command}"


@pytest.mark.parametrize(
    "command, why", CONTRIBUTOR_MUST_RUN.items(), ids=list(CONTRIBUTOR_MUST_RUN)
)
def test_contributing_still_asks_for_it(contributor_commands, command, why):
    assert command in contributor_commands, (
        f"CONTRIBUTING 的 {CONTRIBUTOR_SECTION} 不再让贡献者跑「{why}」：{command}。"
        f"CI 仍然跑它，于是本地全绿会在 PR 上变红"
    )


def test_contributors_are_only_asked_for_checks_ci_also_runs():
    """反方向也得成立：本地跑了却没人在 CI 上把关的检查，绿不绿全看作者自觉。"""
    orphans = sorted(set(CONTRIBUTOR_MUST_RUN) - set(CI_MUST_RUN))
    assert not orphans, f"这些检查只写给了贡献者，合并门并不跑：{orphans}"


def test_the_difference_between_the_two_lists_is_accounted_for(contributor_section):
    """差集必须是显式的一份清单，并且在文档里说出来。

    往 CI 加一步而不动这里，差集就对不上、门就红：加的人必须选一个桶。选不出来通常说明
    这项检查的定位还没想清楚。
    """
    difference = set(CI_MUST_RUN) - set(CONTRIBUTOR_MUST_RUN)
    assert difference == set(CI_ONLY), (
        "CI 跑而贡献者不跑的那部分和 CI_ONLY 对不上。多出来的要么补进 CONTRIBUTING，"
        f"要么在 CI_ONLY 里写清为什么不要求：{sorted(difference ^ set(CI_ONLY))}"
    )

    missing = sorted(word for word in CI_ONLY.values() if word not in contributor_section)
    assert not missing, (
        f"CONTRIBUTING 的 {CONTRIBUTOR_SECTION} 没提到只有 CI 跑的这几项：{missing}。"
        f"不写出来的话，「CI 跑的是超集」这句话没人核得动"
    )


def test_the_gate_installs_a_pinned_ruff(commands):
    """浮动版本会让同一份代码在不同日子给出不同结论，而门是按天跑的。"""
    assert "pip install ruff==" in commands, "ruff 版本没有钉住"


def test_no_step_is_allowed_to_fail_quietly(commands):
    """`continue-on-error` 会让一步跑完、失败、然后被当成没事发生。"""
    assert "continue-on-error" not in commands, (
        "有步骤带 continue-on-error：它失败了也不会让门变红"
    )
