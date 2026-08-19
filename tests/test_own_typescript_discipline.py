"""本仓自己写的 TypeScript 守 ar-runtime/CLAUDE.md 那条规矩，上游镜像不守。

`ar-runtime/CLAUDE.md` 写着「生产代码禁止 as any」。`ar-runtime/src` 下非测试文件里有
149 处，分布在 77 个文件——但那棵树是反编译来的上游镜像，本仓一处都没新增。在不属于自己
的代码上立门禁只有两条路：改上游（加大 divergence）或者加豁免（门禁形同虚设）。

所以门只管本仓自己写的那几个文件。这跟 Python 侧 `ruff.toml` 的做法是同一个形状：选一个
能立刻绿的范围，进 CI，剩下的开 issue 慢慢清（#31、#32）。

自有文件的判据是文件名前缀 `ar-`：这棵树里所有 AutoResearch 加的东西都这么命名，上游
一个都不叫这个。用 git 比对上游基线也可以，但那要求基线 commit 一直在历史里。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# `as any` 和 `<any>` 两种写法，以及裸 `: any` 的参数/变量声明。
BANNED = re.compile(r"\bas\s+any\b|<any>|:\s*any\b")


def own_typescript() -> list[Path]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "ar-runtime"],
                         capture_output=True, text=True, check=True).stdout.split()
    return [REPO / f for f in out
            if Path(f).name.startswith("ar-") and Path(f).suffix in {".ts", ".tsx"}]


def test_the_gate_has_something_to_guard():
    """自有 TS 一个都找不到时，这道门是绿的但什么也没守。"""
    assert own_typescript(), "没找到任何 ar-*.ts；判据可能失效了"


def test_no_any_in_our_own_typescript():
    offenders = []
    for path in own_typescript():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("//", 1)[0]
            if line.lstrip().startswith("*"):
                continue  # 块注释正文
            if BANNED.search(code):
                offenders.append(f"{path.relative_to(REPO)}:{number}  {line.strip()[:70]}")

    assert not offenders, (
        "本仓自己写的 TypeScript 里出现了 any。响应体这类无类型数据声明成 unknown "
        "再逐层收窄，别用断言把整条链的检查关掉：\n  " + "\n  ".join(offenders))
