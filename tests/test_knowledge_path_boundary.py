"""一个名字能指到哪个文件。

知识文件的正文会整段进提示词，所以「这个名字指到哪」是外发边界，判据是 resolve() 之后的路径
而不是名字的拼写。这里的每一条都对应一次真实的绕过：`../x` 和绝对路径、目录内指向目录外的
symlink、指向 TEMPLATE/README 的别名、大小写变体。

正向控制也在这里：指向同目录另一份文档的 symlink 必须继续可用，否则边界就收得过紧了。"""

from __future__ import annotations

import pytest


def test_readme_and_template_are_not_directions(forge, tmp_path):
    bl = forge.bl
    (tmp_path / "README.md").write_text("# R\n", encoding="utf-8")
    (tmp_path / "TEMPLATE.md").write_text("# T\n", encoding="utf-8")
    bl.KNOWLEDGE_BASE_DIR = tmp_path

    assert bl.available_knowledge_files() == []
    with pytest.raises(ValueError, match="README"):
        bl.select_b_directions(["README"])

@pytest.mark.parametrize("name", ["../THIRD_PARTY_NOTICES", "/tmp/anything", "sub/dir"])
def test_a_name_cannot_reach_outside_knowledge_base(forge, name):
    """The name is joined onto knowledge_base/ and was not checked for escapes.

    `../THIRD_PARTY_NOTICES` resolved, and its first H1 became the direction's
    domain, which format_b_context puts in the prompt sent to the provider. A
    typo in the config was enough; nothing had to be malicious.
    """
    bl = forge.bl
    with pytest.raises(ValueError, match="既不在领域方向库"):
        bl.select_b_directions([name])

def _kb_with_symlink_out(tmp_path):
    """A knowledge_base/ whose entry is a symlink to a document outside it."""
    outside = tmp_path / "outside.md"
    outside.write_text("# OUTSIDE TITLE\nOUTSIDE BODY\n", encoding="utf-8")
    kb = tmp_path / "knowledge_base"
    kb.mkdir()
    (kb / "leak.md").symlink_to(outside)
    return kb

def test_a_symlink_out_of_knowledge_base_is_not_a_direction(forge, tmp_path):
    """Confining the name was not enough: the entry itself can point outside.

    available_knowledge_files() globbed *.md, so the symlink was a legal name;
    load_knowledge() then followed it, and format_b_context() puts that text in
    the prompt. Unlike the `../name` case, this leaks the whole body.
    """
    bl = forge.bl
    bl.KNOWLEDGE_BASE_DIR = _kb_with_symlink_out(tmp_path)

    assert bl.available_knowledge_files() == []
    with pytest.raises(ValueError, match="既不在领域方向库"):
        bl.select_b_directions(["leak"])

def test_an_escaping_symlink_never_reaches_the_prompt(forge, tmp_path):
    """The registry names a file too, so the check cannot only live in selection."""
    bl = forge.bl
    bl.KNOWLEDGE_BASE_DIR = _kb_with_symlink_out(tmp_path)

    ctx = bl.format_b_context({"domain": "d", "problem": "p", "knowledge_md": "leak.md"})
    assert "OUTSIDE BODY" not in ctx
    assert bl.load_knowledge("leak.md") == ""
    assert bl.has_knowledge({"knowledge_md": "leak.md"}) is False

def test_a_symlink_inside_knowledge_base_still_works(forge, tmp_path):
    """Only escaping is the problem. Pointing at a sibling document is fine."""
    bl = forge.bl
    kb = tmp_path / "knowledge_base"
    kb.mkdir()
    (kb / "real.md").write_text("# Real\nbody\n", encoding="utf-8")
    (kb / "alias.md").symlink_to(kb / "real.md")
    bl.KNOWLEDGE_BASE_DIR = kb

    assert bl.available_knowledge_files() == ["alias", "real"]
    assert "body" in bl.load_knowledge("alias.md")

def test_an_alias_for_the_template_is_not_a_direction(forge, tmp_path):
    """NOT_A_DIRECTION was tested against the requested name, not the resolved one.

    A symlink under another name got past it, and the same function then rejected
    the entry it had just produced -- knowledge_md was the resolved TEMPLATE.md,
    so has_knowledge() said False. Nothing escaped the directory; the direction
    just ran with no knowledge, spending three ideation calls per seed on the
    template.
    """
    bl = forge.bl
    kb = tmp_path / "knowledge_base"
    kb.mkdir()
    (kb / "TEMPLATE.md").write_text("# Template\n", encoding="utf-8")
    (kb / "README.md").write_text("# Readme\n", encoding="utf-8")
    (kb / "template_alias.md").symlink_to(kb / "TEMPLATE.md")
    (kb / "readme_alias.md").symlink_to(kb / "README.md")
    bl.KNOWLEDGE_BASE_DIR = kb

    assert bl.available_knowledge_files() == []
    for alias in ("template_alias", "readme_alias"):
        with pytest.raises(ValueError, match="既不在领域方向库"):
            bl.select_b_directions([alias])

def test_a_lowercase_reserved_name_is_still_reserved(forge, tmp_path):
    """The exclusion compared exact bytes against README.md / TEMPLATE.md.

    Does not depend on the filesystem: the documents are created lowercase, so
    the assertion is the same on a case-sensitive one.
    """
    bl = forge.bl
    kb = tmp_path / "knowledge_base"
    kb.mkdir()
    (kb / "template.md").write_text("# Template\n", encoding="utf-8")
    (kb / "readme.md").write_text("# Readme\n", encoding="utf-8")
    (kb / "real.md").write_text("# Real\nbody\n", encoding="utf-8")
    bl.KNOWLEDGE_BASE_DIR = kb

    assert bl.available_knowledge_files() == ["real"]
    for name in ("template", "readme", "Template", "README"):
        assert bl.direction_from_file(name) is None, name

def test_a_case_variant_of_a_reserved_name_is_rejected(forge, tmp_path):
    """On a case-insensitive filesystem `template` opens TEMPLATE.md, and
    Path.resolve() does not restore the directory entry's real case, so the whole
    template came back as a direction with its body. On a case-sensitive one the
    name simply does not exist. Rejected either way."""
    bl = forge.bl
    kb = tmp_path / "knowledge_base"
    kb.mkdir()
    (kb / "TEMPLATE.md").write_text("# Template\n", encoding="utf-8")
    (kb / "README.md").write_text("# Readme\n", encoding="utf-8")
    bl.KNOWLEDGE_BASE_DIR = kb

    for name in ("template", "TEMPLATE", "readme", "README", "ReadMe"):
        assert bl.direction_from_file(name) is None, name
        with pytest.raises(ValueError, match="既不在领域方向库"):
            bl.select_b_directions([name])
