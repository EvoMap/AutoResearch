"""workflow 引用的第三方 action 钉在哪个 commit 上。

`uses: actions/checkout@v4` 这种写法有两个后果。一个是安全的：tag 可以被移动，钉 tag 等于
把「CI 里跑什么代码」的决定权交给上游仓库的写权限。另一个是这个仓库真撞上的：v4 内部跑
Node 20，GitHub 在 2026 年把它标为 deprecated 并强制改跑 Node 24，日志里每次都刷一条警告，
而没有任何一步会因此变红，升级于是一直不发生（#159）。

这个文件挡的是让上面两件事再发生的写法，不是 Node 版本本身。运行时是几，要去问 GitHub，
离线的测试判不了；而「钉死 + 全仓同一个版本」能保证升级是一次有人看过的动作，且改一次就
改全。

行尾的版本注释是给人读的：40 位 sha 看不出新旧，没有它，下次没人知道该不该升。
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# `uses: owner/repo@ref  # comment`。ref 允许出现 sha 之外的东西，正是要在断言里判它。
USES = re.compile(r"^\s*-?\s*uses:\s*(?P<action>[^@\s]+)@(?P<ref>\S+)\s*(?:#\s*(?P<note>.*))?$")

PINNED = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


def references() -> list[tuple[Path, int, str, str, str]]:
    """全部 workflow 里的 action 引用。文件名和行号一起带出来，红的时候能直接定位。"""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            hit = USES.match(line)
            if hit:
                found.append((path, number, hit["action"], hit["ref"], (hit["note"] or "").strip()))
    return found


def test_there_is_something_to_check():
    """引用一条都扫不到时，下面两条会空过。

    这个文件按行读 yaml，改缩进或换成 `uses:` 之外的写法都可能让正则整片失配，而失配的
    表现是全绿。这条是那种情况的唯一提示。
    """
    assert references(), f"{WORKFLOWS} 里没扫到任何 action 引用，正则可能已经和 yaml 对不上"


def test_every_action_is_pinned_to_a_commit():
    loose = [f"{p.name}:{n} {a}@{r}" for p, n, a, r, _ in references() if not PINNED.match(r)]
    assert not loose, "这些 action 钉的是 tag 不是 commit，上游移动 tag 就换了 CI 跑的代码：\n" + "\n".join(loose)


def test_every_pin_says_which_version_it_is():
    silent = [f"{p.name}:{n} {a}@{r[:12]}" for p, n, a, r, note in references() if not VERSION.match(note)]
    assert not silent, "这些引用没在行尾注明版本，40 位 sha 看不出新旧，升级时无从判断：\n" + "\n".join(silent)


def test_one_action_is_not_pinned_to_two_versions():
    """同一个 action 在不同 workflow 里钉不同版本，升级就会漏掉没人想起的那个文件。

    #159 就是这个形态：checkout 在两个文件里是 v6.0.3，在第三个文件里还是 v4，而只有第三个
    文件在刷 deprecation 警告。
    """
    seen: dict[str, dict[str, list[str]]] = {}
    for path, number, action, ref, note in references():
        # 按 ref 分组，不按出现次数：同一个 sha 出现在三个文件里是正常的，那是钉齐了。
        where = seen.setdefault(action, {}).setdefault(f"{note or ref[:12]}", [])
        where.append(f"{path.name}:{number}")

    split = {action: pins for action, pins in seen.items() if len(pins) > 1}
    assert not split, "同一个 action 钉了多个版本，下次升级会漏掉其中一处：\n" + "\n".join(
        f"{action}: " + "; ".join(f"{pin} 在 {', '.join(where)}" for pin, where in sorted(pins.items()))
        for action, pins in split.items()
    )
