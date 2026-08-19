"""注释里不写 `文件:行号`，写符号名。

行号是一张快照。仓里扫出来 24 处这种引用，大部分已经指错了地方——
`check_model_references.py` 的四条（`llm_client.py:527/532/534/230`）实际都偏了十来行，
`preflight.py` 里指向 `llm_client.py:150` 和 `164` 的两条同样。读注释的人会照着行号跳过去，
看到一行毫不相干的代码，然后要么当注释写错了、要么当代码改错了。

符号名不会漂：`llm_client.call_chat_completions()` grep 得到，改名时 grep 也找得到。

`commons/rules/taste.md` 把这条写成通则：不手维护会 drift 的元信息。这个门是它在本仓的
可机检部分。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Every tracked public source file follows the same reference convention.
VENDORED = ()
SCANNED_SUFFIXES = {".py", ".md", ".toml", ".json", ".sh"}

REFERENCE = re.compile(r"[A-Za-z_][A-Za-z0-9_/.-]*\.(?:py|ts):\d+")

# 合成路径，本身就是用例数据，不指向任何真实位置。
FIXTURES = {"src/x.py:1", "scripts/x.ts:1"}


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                         capture_output=True, text=True, check=True).stdout.split()
    return [REPO / f for f in out
            if not f.startswith(VENDORED) and Path(f).suffix in SCANNED_SUFFIXES]


def test_no_comment_points_at_a_line_number():
    offenders = []
    for path in tracked_files():
        if path.resolve() == Path(__file__).resolve():
            continue  # 这个文件本身要举反例
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for hit in REFERENCE.findall(line):
                if hit in FIXTURES:
                    continue
                offenders.append(f"{path.relative_to(REPO)}:{number}  {hit}")

    assert not offenders, (
        "这些地方用行号指代码，行号会漂。改成符号名（函数名 / 常量名）：\n  "
        + "\n  ".join(offenders))
