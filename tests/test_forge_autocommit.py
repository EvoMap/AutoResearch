"""跑批任务在别人正在工作的仓里跑，所以它的提交只能包含它自己那两个文件。

2026-08-11 真发生过：一次 `git rm` 还暂存着没提交，这个任务跑过来，不带 pathspec 的
`git commit` 把它卷进了一个叫「Pending forge」的提交（`de94d1c`，572 行删除，与 forge
毫无关系）。

这里不 import 那个模块——它在 import 期就会拉起整条流水线。判据落在真实 git 行为上：
同样的命令序列，在一个有别人暂存内容的仓里跑，那些内容必须留在索引里。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "run_pending_forge.py"


def git(cwd: Path, *argv, check=True):
    return subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True,
                          text=True, check=check)


def sandbox(
    tmp_path: Path,
    page_names=("index.html", "ideas.html"),
    ignore_pages=False,
) -> Path:
    git(tmp_path, "init", "-q")
    # 文件名用 ASCII：git 默认按 core.quotePath 转义非 ASCII 路径，
    # 断言会被一个与被测行为无关的配置绊住。
    for name in (*page_names, "someone-else.txt"):
        (tmp_path / name).write_text("v1\n", encoding="utf-8")
    if ignore_pages:
        (tmp_path / ".gitignore").write_text(
            "/index.html\n/ideas.html\n",
            encoding="utf-8",
        )
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    return tmp_path


def load_script():
    spec = importlib.util.spec_from_file_location("run_pending_forge", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_pathspec_commit_leaves_other_staged_work_alone(tmp_path):
    """判据是「别人暂存的东西还在索引里」，不是「提交成功了」。"""
    repo = sandbox(tmp_path)
    (repo / "index.html").write_text("v2\n", encoding="utf-8")
    (repo / "someone-else.txt").write_text("我正在改\n", encoding="utf-8")
    git(repo, "add", "someone-else.txt")

    git(repo, "add", "index.html", "ideas.html")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "Pending forge", "--", "index.html", "ideas.html")

    landed = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert landed == ["index.html"], landed
    staged = git(repo, "diff", "--cached", "--name-only").stdout.split()
    assert "someone-else.txt" in staged, "别人的改动被卷走了"


def test_without_the_pathspec_it_does_get_swept_in(tmp_path):
    """负控：这就是当时发生的事。没有它，上面那条证明不了 pathspec 起了作用。"""
    repo = sandbox(tmp_path)
    (repo / "index.html").write_text("v2\n", encoding="utf-8")
    (repo / "someone-else.txt").write_text("我正在改\n", encoding="utf-8")
    git(repo, "add", "someone-else.txt")

    git(repo, "add", "index.html", "ideas.html")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "Pending forge")

    landed = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert "someone-else.txt" in landed, "负控没复现出问题，这条门就是空的"


# ---- 脚本自己 ----

SOURCE = SCRIPT.read_text(encoding="utf-8")


def test_the_commit_carries_a_pathspec():
    assert re.search(r'"commit",\s*"-m",\s*message,\s*"--",\s*\*pages', SOURCE), \
        "commit 要带 pathspec"


def test_every_git_step_checks_its_return_code():
    """原来三步的返回码全丢掉，push 失败照样打印「Git push 完成」。"""
    tail = SOURCE[
        SOURCE.index("def publish_generated_pages"):SOURCE.index("def main")
    ]

    for step in ("add", "commit", "push"):
        assert f"{step}.returncode != 0" in tail or "done.returncode != 0" in tail, \
            f"{step} 没查返回码"
    assert tail.count("returncode != 0") >= 3


def test_the_push_names_the_branch_it_means():
    """不写 refspec 时推的是当前分支的 HEAD，而这个脚本不挑分支。"""
    assert '"push", "origin", "HEAD:gh-pages"' in SOURCE


def test_nothing_to_commit_is_not_reported_as_a_failure():
    """两个页面没变化是常态，不该每天报一次错。"""
    assert "nothing to commit" in SOURCE


def test_publish_accepts_a_clean_clone_without_index_html(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sandbox(repo, page_names=(), ignore_pages=True)
    (repo / "ideas.html").write_text("generated\n", encoding="utf-8")
    assert git(repo, "check-ignore", "ideas.html").stdout.strip() == "ideas.html"
    (repo / "someone-else.txt").write_text("staged by someone else\n", encoding="utf-8")
    git(repo, "add", "someone-else.txt")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(repo, "remote", "add", "origin", str(remote))

    module = load_script()
    messages = []
    module.log = messages.append
    module.publish_generated_pages(repo)

    landed = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert landed == ["ideas.html"], landed
    staged = git(repo, "diff", "--cached", "--name-only").stdout.split()
    assert staged == ["someone-else.txt"], staged
    assert "refs/heads/gh-pages" in git(repo, "ls-remote", "--heads", "origin").stdout
    assert messages[-1] == "Git push 完成"


def test_publish_skips_when_no_page_was_generated(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sandbox(repo, page_names=())
    before = git(repo, "rev-parse", "HEAD").stdout.strip()

    module = load_script()
    messages = []
    module.log = messages.append
    module.publish_generated_pages(repo)

    assert git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert messages == ["没有生成页面，跳过 Git 提交"]
