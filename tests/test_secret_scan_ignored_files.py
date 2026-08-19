"""git 忽略的文件不进扫描，因为它们不会被发布。

README 的第一条命令是 `bash scripts/bringup.sh`，接着才创建 `.env` 并填真凭据；用户补完
凭证后还会再次运行 bringup。bringup 用 `secret_scan.py .` 扫整个工作目录，于是那份按设计
存在、按设计被忽略的 `.env` 曾被判成发布风险（#49）。

这个脚本的定位写在它自己的 docstring 第一行：sanitized release tree 的发布前守卫。
所以判据是「这个文件会不会被发布」，不是「它在不在磁盘上」：

  git 忽略        不扫——发布不了，扫了只会教人把守卫关掉
  git 未跟踪但不忽略  要扫——一次 git add 就会被发布
  git 已跟踪       要扫——真 key 粘进被跟踪的文件，正是这个守卫的目标
  不在 git 仓里     全扫——release tree 是 git archive 出来的，那里没有 .git

「没人能弄绿的门会被关掉」是这个仓库自己的教训，写在 check_model_references.py 的
docstring 里。一条按 README 操作就必然红的门属于这一类。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCAN = REPO / "scripts" / "secret_scan.py"

# 命中 openai-style-key 规则的形状，值本身是假的。
FAKE_KEY = "sk-" + "A1b2C3d4E5f6G7h8" * 3


def run(root: Path):
    # 带一份空 allowlist：本仓那 24 条是给本仓的，拿去扫临时目录会全部判成陈旧条目，
    # 于是退出码红的原因不是要测的那个。
    allowlist = root.parent / "allowlist.json"
    allowlist.write_text('{"allow": []}', encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(SCAN), str(root), "--allowlist", str(allowlist)],
        capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout + done.stderr


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def test_an_ignored_file_is_not_a_release_risk(tmp_path: Path):
    root = make_repo(tmp_path)
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / ".env").write_text(f"OPENAI_API_KEY={FAKE_KEY}\n", encoding="utf-8")

    code, out = run(root)

    assert code == 0, out
    assert ".env" not in out


def test_an_untracked_but_publishable_file_is_still_scanned(tmp_path: Path):
    """没被忽略只是还没 git add，一次 add 就会被发布。"""
    root = make_repo(tmp_path)
    (root / "leaked.py").write_text(f'KEY = "{FAKE_KEY}"\n', encoding="utf-8")

    code, out = run(root)

    assert code == 1
    assert "leaked.py" in out


def test_a_tracked_file_is_still_scanned(tmp_path: Path):
    root = make_repo(tmp_path)
    (root / "tracked.py").write_text(f'KEY = "{FAKE_KEY}"\n', encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=root, check=True)

    code, out = run(root)

    assert code == 1
    assert "tracked.py" in out


def test_outside_a_git_repo_everything_is_scanned(tmp_path: Path):
    """release tree 是 git archive 出来的，那里没有 .git，一个文件都不能漏。"""
    root = tmp_path / "release"
    root.mkdir()
    (root / ".env").write_text(f"OPENAI_API_KEY={FAKE_KEY}\n", encoding="utf-8")

    code, out = run(root)

    assert code == 1
    assert ".env" in out


def test_the_scan_says_how_many_it_skipped(tmp_path: Path):
    """静默跳过和静默放行一样危险：要能看出这次少扫了什么。"""
    root = make_repo(tmp_path)
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / ".env").write_text(f"OPENAI_API_KEY={FAKE_KEY}\n", encoding="utf-8")

    _, out = run(root)

    assert "git 忽略" in out and "1" in out, out


def test_a_tree_that_is_entirely_ignored_is_not_a_pass(tmp_path: Path):
    """把 release tree 解到仓库内部时 .gitignore 会整个吃掉它，一个字节没读却报绿。"""
    root = make_repo(tmp_path)
    (root / ".gitignore").write_text("release/\n", encoding="utf-8")
    (root / "release").mkdir()
    (root / "release" / "app.py").write_text(f'KEY = "{FAKE_KEY}"\n', encoding="utf-8")

    code, out = run(root / "release")

    assert code != 0
    assert "nothing was scanned" in out, out


def test_a_clone_under_a_skipped_directory_name_is_still_scanned(tmp_path):
    """SKIP_DIRS 里是 build / dist / venv 这类常见目录名。

    判断落在绝对路径上时，clone 放在 `~/build/` 或 `/data/dist/` 下，整棵树的每个文件
    都命中跳过规则——扫描一个字节都不读却报绿，比报红危险得多。同一个形状在
    `test_requirements_match_imports.py` 上也发作过（#133）。
    """
    root = tmp_path / "build" / "AutoResearch"
    (root / "src").mkdir(parents=True)
    (root / "src" / "leaky.py").write_text('KEY = "sk-' + "x" * 32 + '"\n', encoding="utf-8")

    code, output = run(root)

    assert code != 0, f"整棵树被跳过了，什么都没扫到：\n{output}"
    assert "leaky.py" in output


def test_a_real_skipped_directory_inside_the_tree_still_skips(tmp_path):
    """修正不能把跳过规则整个废掉：树内部真的有 node_modules 时仍要跳。"""
    root = tmp_path / "repo"
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "leak.js").write_text(
        'const k = "sk-' + "y" * 32 + '";\n', encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")

    code, output = run(root)

    assert code == 0, f"node_modules 里的东西不该被扫：\n{output}"
