"""pre-commit 和 CI 必须跑同一套检查的同一个版本。

两处各钉一次版本号，迟早分岔：本地绿、CI 红，或者更糟，本地红了但 CI 用的是另一个版本
所以放行。这条门就是那个对账。

也检查 hook 引用的脚本真的存在——重命名一个脚本而忘了改 hook，只会在别人下次提交时
才炸，而且报的是「找不到文件」，不是「你的改动有问题」。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = (REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8")
WORKFLOW = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_ruff_is_pinned_to_the_same_version_as_ci():
    in_ci = re.search(r"ruff==([\d.]+)", WORKFLOW)
    in_hook = re.search(r"ruff-pre-commit\n\s*rev:\s*v([\d.]+)", CONFIG)

    assert in_ci and in_hook, "两边都要钉版本"
    assert in_ci.group(1) == in_hook.group(1), (
        f"CI 用 ruff {in_ci.group(1)}，pre-commit 用 {in_hook.group(1)}")


def test_every_script_a_hook_runs_exists():
    for script in re.findall(r"entry: \.venv/bin/python (\S+)", CONFIG):
        assert (REPO / script).exists(), f"hook 引用了不存在的 {script}"


def test_no_hook_rewrites_files():
    """改写型 hook 会让提交的状态和被检查的状态分岔。

    这个仓已经吃过一次：`bun run check` 触发同名的 `precheck` 生命周期钩子，里面的
    `check:fix` 改了 15 个文件，CI 绿的是改完之后的状态而不是提交的状态。
    """
    rewriting = {"end-of-file-fixer", "trailing-whitespace", "ruff-format",
                 "black", "isort", "pretty-format-json"}

    used = set(re.findall(r"- id: (\S+)", CONFIG))

    assert not (used & rewriting), f"这些 hook 会改文件：{used & rewriting}"


def test_ruff_check_does_not_carry_fix():
    """`ruff-check` 加 `--fix` 就变成改写型，上面那条按 id 判就看不见了。"""
    assert "--fix" not in CONFIG, "ruff 的 --fix 会改文件"


def test_the_slow_scan_is_not_on_the_commit_path():
    """secret_scan 要 3.2s。慢 hook 会被 --no-verify 掉，那等于没有。"""
    section = CONFIG[CONFIG.index("id: secret-scan"):]
    assert "stages: [pre-push]" in section[:400], "secret_scan 要留在 pre-push"


def test_bringup_runs_the_same_tests_as_ci():
    """本地绿不代表 CI 绿，是这个仓反复踩的形状，这次踩在自己脚上。

    CI 接上 `ar-runtime/scripts/tests/` 时忘了同步 bringup，于是 bringup 报 617、
    CI 跑 629——差的正好是 workflow engine 的协议测试，而那是新 clone 第一条命令给出
    的唯一质量信号。
    """
    import re

    bringup = (REPO / "scripts" / "bringup.sh").read_text(encoding="utf-8")

    ci_line = re.search(r"python -m pytest ([^|]+?) -q", WORKFLOW)
    # bringup 按目录存在与否过滤，所以它跑什么写在候选清单里，不在命令行上——
    # 读命令行只会读到一个变量名。
    candidates = re.search(r"for candidate in ([^;]+); do", bringup)

    assert ci_line and candidates, "两边都要能读出跑了哪些路径"
    assert set(ci_line.group(1).split()) == set(candidates.group(1).split()), (
        f"CI 跑 {ci_line.group(1).split()}，bringup 候选是 {candidates.group(1).split()}")


def test_no_hook_hardcodes_an_interpreter_path():
    """写死 `.venv/bin/python` 的 hook 在 linked worktree 里必然失败。

    git 不复制 ignored 目录，所以那个树没有 `.venv`，hook 在检查任何代码之前就报
    「Executable not found」——而这个仓自己要求用 worktree 并行开发（#119）。
    """
    # 判据落在 entry 行上：注释里为了解释这条规则会提到那个路径，扫全文会命中自己。
    entries = re.findall(r"entry: (\S+)", CONFIG)

    hardcoded = [e for e in entries if e.startswith(".venv/") or e.startswith("/")]
    assert not hardcoded, f"这些 entry 写死了解释器路径：{hardcoded}"


def test_the_hook_launcher_looks_in_both_trees():
    """linked worktree 用得上主工作树的 venv，那是它唯一现成的解释器。"""
    launcher = (REPO / "scripts" / "hook_python.sh").read_text(encoding="utf-8")

    assert "git rev-parse --show-toplevel" in launcher, "先看这个树自己的"
    assert "git rev-parse --git-common-dir" in launcher, "再看主工作树的"


def test_the_launcher_refuses_rather_than_skipping():
    """跳过的 hook 和没有 hook 一样。找不到解释器要非零退出并说清怎么办。"""
    launcher = (REPO / "scripts" / "hook_python.sh").read_text(encoding="utf-8")

    assert "exit 1" in launcher
    assert "bringup.sh" in launcher, "要给出下一步"


def test_the_launcher_checks_the_interpreter_can_import_the_dependencies():
    """系统 python 存在不代表能跑：检查脚本 import httpx。"""
    launcher = (REPO / "scripts" / "hook_python.sh").read_text(encoding="utf-8")

    assert "import httpx" in launcher
