"""ruff 的 exclude 只准挡住别人的代码。

`exclude` 按目录生效，而目录和代码归属不是一回事。`claude-code` 曾整个挂在 exclude 里，
理由写的是"vendored tree 自带 tooling"——但那棵树下 10 个 Python 文件里有 9 个是本仓
自己写的：workflow engine、monitor，和 7 个协议测试。一条为了避开反编译镜像而写的规则，
顺手把引擎也摘出去了，于是它从来没被 lint 过（#229）。

所以门比对两处活的来源：`git ls-files` 说仓库里有哪些 Python，`ruff check --show-files`
说 linter 实际会看哪些。差集必须落在下面这份记着理由的清单里。清单按目录记，因为
exclude 本来就是按目录写的；要新增一条，得在这里写清为什么那整个目录都不是本仓的代码。

不去解析 ruff.toml 自己算一遍 exclude 语义：那是拿一份仿制品当真相源，ruff 的匹配规则
（gitignore 语义、目录 vs 前缀）一变，仿制品就开始撒谎。问 ruff 本人。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Public releases do not exclude any tracked Python source from lint.
NOT_OURS = {}

# 引擎和它的协议测试都在这里。这是 #229 修好的那条，单独钉住：
# 上面的差集检查只保证"没有本仓代码被排除"，钉死路径才能保证是这几个文件。
MUST_BE_LINTED = "ar-runtime/scripts"


def ruff_argv() -> list[str]:
    """优先用跑测试的这个解释器里的 ruff：CI 就是往同一个环境里装的。"""
    if subprocess.run([sys.executable, "-m", "ruff", "--version"],
                      capture_output=True).returncode == 0:
        return [sys.executable, "-m", "ruff"]
    on_path = shutil.which("ruff")
    assert on_path, "找不到 ruff。装不上 linter 时这道门无从判断，不能当作通过"
    return [on_path]


def files_ruff_checks() -> set[str]:
    out = subprocess.run([*ruff_argv(), "check", "--show-files", "."],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout
    return {str(Path(line).resolve().relative_to(REPO)) for line in out.split() if line}


def tracked_python() -> set[str]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.py"],
                         capture_output=True, text=True, check=True).stdout
    return set(out.split())


def test_every_unlinted_python_file_is_someone_elses():
    unlinted = tracked_python() - files_ruff_checks()

    unexplained = sorted(f for f in unlinted
                         if not any(f == root or f.startswith(root + "/") for root in NOT_OURS))
    assert not unexplained, (
        "这些 Python 文件在仓库里，但 ruff 不看它们。要么把它们纳入 lint，要么在 NOT_OURS "
        f"里记一笔说明为什么那不是本仓的代码：{unexplained}")


def test_the_engine_and_its_protocol_tests_are_linted():
    """按目录排除的代价是本仓代码会被顺手带走，所以正面钉住这一处。"""
    ours = {f for f in tracked_python() if f.startswith(MUST_BE_LINTED + "/")}
    assert ours, f"{MUST_BE_LINTED} 下一个 Python 都没有，判据可能失效了"

    missed = sorted(ours - files_ruff_checks())
    assert not missed, f"{MUST_BE_LINTED} 下的文件被排除在 lint 之外：{missed}"


def test_a_recorded_exclusion_that_no_longer_matches_anything_shows_up():
    """清单只准变短。留一条早已不存在的目录，它就慢慢变成没人看的免罪符。"""
    tracked = tracked_python()

    stale = sorted(root for root in NOT_OURS
                   if not any(f.startswith(root + "/") or f == root for f in tracked))
    assert not stale, f"这些目录下已经没有 Python 了，把它们从 NOT_OURS 里删掉：{stale}"
