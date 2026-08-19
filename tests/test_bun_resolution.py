"""Where does this repository think bun is, and does everyone ask the same place?

bun installs to ~/.bun/bin and puts that on PATH for interactive shells only, so
"installed" and "on PATH" are different facts on the same machine. Every call site
that decided this for itself decided it differently, and the two halves disagreed
in the worst possible way: #201 was a test that spawned bare argv and died with
FileNotFoundError while the test beside it, resolving the same binary its own way,
passed; #206 was the import-scan gate telling a machine to install what it already
had, with the gate's own test green throughout.

So there is one resolver now, and these tests hold both ends of that: scripts/find-bun.sh
answers the three states correctly, and nobody goes around it.
"""

from __future__ import annotations

import ast
import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FINDER_REL = "scripts/find-bun.sh"
FINDER = REPO / FINDER_REL


def fake_bun(directory: Path) -> Path:
    """A file that is executable and never run: resolution is a lookup, not a call."""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "bun"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def find(path_dir: Path, home: Path) -> subprocess.CompletedProcess:
    """PATH 里只有交给它的那一个目录，机器上真装没装 bun 影响不了结论。

    走 /bin/sh 而不是 shebang：shebang 是 `/usr/bin/env sh`，env 要在 PATH 上找 sh，而
    这里的 PATH 是空的，于是每条都以 127 收场——测的就成了别的东西。执行位另有一条盯着。
    """
    return subprocess.run(
        ["/bin/sh", str(FINDER)], capture_output=True, text=True,
        env={"PATH": str(path_dir), "HOME": str(home)},
    )


def test_bun_on_path_reports_it_and_exits_zero(tmp_path: Path) -> None:
    on_path = fake_bun(tmp_path / "bin")

    found = find(tmp_path / "bin", tmp_path / "home")

    assert found.returncode == 0
    assert found.stdout.strip() == str(on_path)


def test_bun_installed_but_off_path_is_its_own_state(tmp_path: Path) -> None:
    """Neither 0 nor 1.

    Callers that spawn bun themselves work fine here, and callers that hand the name
    to somebody else's PATH -- ar-runtime/.mcp.json launches its MCP servers with
    `"command": "bun"` -- do not. Collapsing this into "not installed" sends the
    operator to the installer, which writes ~/.bun/bin again and changes nothing.
    """
    home = tmp_path / "home"
    installed = fake_bun(home / ".bun" / "bin")
    (tmp_path / "empty").mkdir()

    found = find(tmp_path / "empty", home)

    assert found.returncode == 3
    assert found.stdout.strip() == str(installed)


def test_no_bun_anywhere_says_nothing_and_fails(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "home").mkdir()

    found = find(tmp_path / "empty", tmp_path / "home")

    assert found.returncode == 1
    assert found.stdout.strip() == ""


def test_path_wins_when_both_have_one(tmp_path: Path) -> None:
    """PATH is what the rest of the toolchain will use, so it is the answer to give."""
    on_path = fake_bun(tmp_path / "bin")
    home = tmp_path / "home"
    fake_bun(home / ".bun" / "bin")

    found = find(tmp_path / "bin", home)

    assert found.returncode == 0
    assert found.stdout.strip() == str(on_path)


# 下面两条盯的是「有没有人自己找」，不是「找得对不对」——找法对不对由上面四条负责。
#
# 逐个形状列，不做「凡是提到 bun 的都算」：注释和给人看的错误信息里出现 bun 是正常的，
# 把它们一起判红，门迟早被绕过去。
FORBIDDEN_SHELL = ("command -v bun", "which bun", ":-bun}")


def tracked(*globs: str) -> list[str]:
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", *globs],
                            capture_output=True, text=True, check=True)
    return listed.stdout.split()


SPAWNERS = ("run", "Popen", "check_output", "check_call", "call")


def resolves_bun_itself(source: str) -> str | None:
    """AST 而不是正则，理由和导入扫描用 bun 的 parser 一样：正则分不清代码和注释。

    只认「起进程时的 argv 首元素」，不认所有以 "bun" 开头的字面量序列：
    历史上的 check_vendored_imports.py 里有个 bun 元组是 package.json
    的 exports 字段名，判它红只会教人把这条门关掉。
    """
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        called = (node.func.attr if isinstance(node.func, ast.Attribute)
                  else getattr(node.func, "id", ""))
        first = node.args[0]
        if called == "which" and isinstance(first, ast.Constant) and first.value == "bun":
            return "which(bun)"
        if (called in SPAWNERS and isinstance(first, (ast.List, ast.Tuple)) and first.elts
                and isinstance(first.elts[0], ast.Constant)
                and first.elts[0].value == "bun"):
            return "argv starts with a literal bun"
    return None


def test_no_python_file_resolves_bun_on_its_own() -> None:
    """再写一处就红在这里，而不是红在某台机器上一条看不懂的 FileNotFoundError。"""
    offenders = {rel: shape for rel in tracked("*.py")
                 if (shape := resolves_bun_itself((REPO / rel).read_text(encoding="utf-8")))}

    assert not offenders, f"这些地方自己找 bun，应该问 {FINDER_REL}：{offenders}"


def test_no_shell_script_resolves_bun_on_its_own() -> None:
    offenders = []
    for rel in tracked("*.sh"):
        if rel == FINDER_REL:
            continue
        text = (REPO / rel).read_text(encoding="utf-8")
        offenders += [(rel, shape) for shape in FORBIDDEN_SHELL if shape in text]

    assert not offenders, f"这些地方自己找 bun，应该问 {FINDER_REL}：{offenders}"


def test_the_finder_is_executable() -> None:
    """Callers run it as a command; a mode bit lost in review turns every one of them red."""
    assert os.access(FINDER, os.X_OK), f"{FINDER_REL} 没有执行位"
