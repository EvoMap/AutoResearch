"""知识文档整份进提示词，所以文件里不该有跑批留下的日志尾巴。

`format_b_context(include_full_knowledge=True)` 把选中方向的 `.md` 原样贴进构思提示词，
抬头写着「社区深度知识 - 必读！」。76 份文档的结尾却带着 `run_research.py` 的 stdout：
一个美元成本数字，和一个指向 `gpt-researcher/outputs/` 的 Python 字典。那个目录被
gitignore，任何 ref 上都没有，照着脚注去找原件的人找不到，而模型在领域知识里读到的是
这两样东西。

`===== SOURCES =====` 不在此列：那是这份综述的实际来源，留着有用。
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
KB = REPO / "knowledge_base"

# `run_research.py` 打给操作者看的分隔标记。它们进得了知识文档，只因为整理那一步是
# 人工复制 stdout。
STDOUT_MARKERS = ("===== COST =====", "===== FILES =====")

# 这两份不是方向文档，`b_library` 也解析不到它们。README 讲的正是「整理时要删掉哪两块」，
# 它必须能把标记原样写出来。
NOT_A_DIRECTION = {"README.md", "TEMPLATE.md"}


def directions() -> list[Path]:
    return [doc for doc in sorted(KB.glob("*.md")) if doc.name not in NOT_A_DIRECTION]


def test_no_knowledge_doc_carries_a_batch_run_stdout_tail():
    dirty = {doc.name: [m for m in STDOUT_MARKERS if m in doc.read_text(encoding="utf-8")]
             for doc in directions()}
    dirty = {name: marks for name, marks in dirty.items() if marks}

    assert not dirty, f"这些文档带着跑批日志尾巴，整份会进提示词：{dirty}"


def test_no_knowledge_doc_points_at_the_ignored_outputs_directory():
    """出处要指得到东西。`gpt-researcher/outputs/` 一个文件都没有进过仓库。"""
    assert not (REPO / "gpt-researcher" / "outputs").exists() or \
        not any((REPO / "gpt-researcher" / "outputs").iterdir()), \
        "outputs/ 现在有内容了，这条断言的前提要重写"

    dangling = [doc.name for doc in directions()
                if "gpt-researcher/outputs/" in doc.read_text(encoding="utf-8")]

    assert not dangling, f"这些文档的出处指向仓里没有的路径：{dangling}"
