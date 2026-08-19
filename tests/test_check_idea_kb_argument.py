"""check_idea 的 --kb 和 --list 必须给出同一个答案。

--kb 被拒时提示「用 --list 看可用的」。两边曾经各自枚举：--list 直接 glob 目录，于是列出了
--kb 随后必然拒绝的项；--kb 用 KB_DIR / 参数拼路径，而参数是绝对路径时 pathlib 会把 KB_DIR
整个丢掉。"""

from __future__ import annotations

import sys

import pytest

from conftest import REPO


def test_check_idea_refuses_a_kb_path_outside_the_directory(tmp_path, monkeypatch):
    """--kb is documented as a name under knowledge_base/, and pathlib drops the
    directory entirely when the argument is absolute."""
    sys.path.insert(0, str(REPO))
    import check_idea

    outside = tmp_path / "abs_secret.md"
    outside.write_text("# ABS TITLE\nABS BODY\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        check_idea.load_kb_content(str(outside))
    assert "knowledge_base" in str(exit_info.value)

def test_check_idea_lists_only_what_it_will_accept(tmp_path, monkeypatch):
    """--list told the user to look there, and listed an entry --kb then refused."""
    sys.path.insert(0, str(REPO))
    import check_idea
    import idea_forge.b_library as bl

    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    kb = tmp_path / "knowledge_base"
    kb.mkdir()
    (kb / "real.md").write_text("# Real\n", encoding="utf-8")
    (kb / "leak.md").symlink_to(outside)
    monkeypatch.setattr(check_idea, "KB_DIR", kb)
    monkeypatch.setattr(bl, "KNOWLEDGE_BASE_DIR", kb)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.append(" ".join(map(str, a))))
    check_idea.list_kb()
    monkeypatch.undo()

    body = "\n".join(printed)
    assert "real" in body
    assert "leak" not in body, "--list offered a name --kb rejects"
