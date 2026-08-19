"""闸门判不了的时候记什么。

四条路走到「没有得出判定」：b_id 不在库里、知识文件读不到、文件缺要对照的两节、调用失败或
回复里读不出判定。它们原来一律返回通过，产出里记 passed=true，和「评审看过并放行」分不出。

不能改成拦下：走到这一步，Step 1 的三次构思和 Step 2 的九次互评已经花掉了，因缺材料丢弃扔的
是已付的钱，换不到信息。所以是三态，unchecked 带标记继续。

不联网。每条用例跑真实知识文件或合成文本，不调模型。

名字指到哪个文件在 test_knowledge_path_boundary.py；选哪些方向在 test_b_direction_selection.py。
"""

from __future__ import annotations

import pytest

from conftest import COMPLETE_KNOWLEDGE, REPO


@pytest.fixture
def cc(forge):
    """The consensus_check the rest of this file talks to."""
    forge.llm.call_role = lambda role, prompt, **k: ""
    return forge.cc


@pytest.fixture
def lib(forge):
    return forge.bl


def test_public_documents_satisfy_the_checklist_contract(cc, lib):
    """Every synthetic document should exercise a real consensus review."""
    library = lib.get_b_library()
    checkable = [
        b for b in library
        if (text := lib.load_knowledge(b["knowledge_md"])) and not cc.missing_sections(text)
    ]
    assert checkable == library


def test_section_match_is_by_heading_not_by_the_prompt_wording(cc, lib):
    """No file spells the sections the way the prompt does.

    The prompt says 常见错误直觉（避坑）and 可行创新切入点清单; the two compliant
    files say `## 3. 常见误区 / 错误直觉` and `## 5. 可行的创新切入点`. A literal
    substring test for the prompt's wording would reject all four.
    """
    good = lib.load_knowledge("agent_memory.md")
    assert good, "agent_memory.md is expected to be present"
    assert "常见错误直觉（避坑）" not in good
    assert cc.missing_sections(good) == []


def test_a_heading_with_no_entries_does_not_count(cc):
    """A section that exists as a header gives the reviewer nothing to compare."""
    empty = "# 领域\n\n## 3. 常见误区 / 错误直觉\n\n## 5. 可行的创新切入点\n\n## 6. 参考\n"
    assert set(cc.missing_sections(empty)) == {"错误直觉(有标题无条目)", "创新切入点(有标题无条目)"}


def test_a_short_but_real_list_counts(cc):
    """Content is judged by having entries, not by length.

    A character threshold would reject a complete three-item list and accept a
    long paragraph that lists nothing, and no threshold value would have evidence
    behind it.
    """
    short = ("# 领域\n\n## 3. 常见误区 / 错误直觉\n"
             "- 误区一\n- 误区二\n- 误区三\n\n"
             "## 5. 可行的创新切入点\n- 切入点一\n- 切入点二\n")
    assert cc.missing_sections(short) == []

    wordy = ("# 领域\n\n## 3. 常见误区 / 错误直觉\n" + "这一节还没有写。" * 40 +
             "\n\n## 5. 可行的创新切入点\n" + "待补充。" * 40 + "\n")
    assert set(cc.missing_sections(wordy)) == {"错误直觉(有标题无条目)", "创新切入点(有标题无条目)"}


def test_persisted_passed_field_stays_a_bool(cc, lib, monkeypatch, tmp_path):
    """A truthy string in `passed` reads as a pass to any consumer.

    The gate exists because "could not check" was indistinguishable from "checked
    and cleared"; putting the third state into that same field would rebuild the
    defect one layer down.
    """
    monkeypatch.setattr(cc.time, "sleep", lambda *a: None)
    (tmp_path / "llm_reasoning.md").write_text("# incomplete\n", encoding="utf-8")
    monkeypatch.setattr(lib, "KNOWLEDGE_BASE_DIR", tmp_path)
    item = {"b_id": "llm_reasoning", "idea_text": "x", "b_domain": "d", "source_model": "m"}
    cc.filter_by_consensus([item])

    record = item["consensus_check"]
    assert record["passed"] is False
    assert record["status"] == "unchecked"
    # The way a consumer would obviously write it.
    assert not record["passed"]


def test_a_knowledge_gap_is_unavailable_not_a_pass(cc, lib, monkeypatch, tmp_path):
    (tmp_path / "llm_reasoning.md").write_text("# incomplete\n", encoding="utf-8")
    monkeypatch.setattr(lib, "KNOWLEDGE_BASE_DIR", tmp_path)
    verdict, reason = cc.consensus_check({"b_id": "llm_reasoning", "idea_text": "x"})
    assert verdict is cc.UNAVAILABLE
    assert "缺 错误直觉" in reason


def test_an_unknown_direction_is_rejected(cc):
    """Outside the library is a pipeline bug, not a user choice."""
    verdict, reason = cc.consensus_check({"b_id": "no_such_direction", "idea_text": "x"})
    assert verdict is False
    assert "B 库里没有" in reason


def test_unchecked_goes_on_and_rejected_does_not(cc, lib, monkeypatch, capsys, tmp_path):
    """Step 1 and Step 2 are already paid for by the time this runs."""
    monkeypatch.setattr(cc.time, "sleep", lambda *a: None)
    (tmp_path / "llm_reasoning.md").write_text("# incomplete\n", encoding="utf-8")
    monkeypatch.setattr(lib, "KNOWLEDGE_BASE_DIR", tmp_path)
    unchecked = {"b_id": "llm_reasoning", "idea_text": "a", "b_domain": "d", "source_model": "m"}
    rejected = {"b_id": "no_such_direction", "idea_text": "b", "b_domain": "d", "source_model": "m"}

    assert cc.filter_by_consensus([unchecked, rejected]) == [unchecked]
    assert unchecked["consensus_check"] == {
        "passed": False, "status": "unchecked",
        "reason": unchecked["consensus_check"]["reason"],
    }
    assert rejected["consensus_check"]["status"] == "reject"
    assert "未做检查: 1" in capsys.readouterr().out


def test_a_missing_knowledge_file_reads_as_empty(lib):
    """Absence is a normal state now, so it is reported by the caller, not here."""
    assert lib.load_knowledge("definitely_not_here.md") == ""


def test_no_caller_treats_the_truthy_sentinel_as_a_pass(cc):
    """UNAVAILABLE is a truthy object, so a plain `if ok` reads it as cleared."""
    source = (REPO / "src" / "idea_forge" / "consensus_check.py").read_text(encoding="utf-8")
    for block in source.split("def ")[1:]:
        if "consensus_check(" in block and "ok" in block:
            assert "if ok:" not in block, "a bare truth test on the sentinel"
    assert source.count("is UNAVAILABLE") >= 1


def test_an_idea_the_check_could_not_read_is_forwarded_and_marked(forge, tmp_path):
    """Blocking it would throw away the ideation and cross-review already paid for."""
    bl = forge.bl
    cc = forge.cc
    (tmp_path / "agent_memory.md").write_text("# 无关内容\n", encoding="utf-8")
    bl.KNOWLEDGE_BASE_DIR = tmp_path

    idea = {"b_id": "agent_memory", "idea_text": "x", "b_domain": "d", "source_model": "m"}
    forwarded = cc.filter_by_consensus([idea])

    assert forwarded == [idea], "an unchecked idea must still reach Step 3"
    assert idea["consensus_check"]["status"] == "unchecked"
    assert idea["consensus_check"]["passed"] is False, "unchecked must not read as cleared"

def test_a_b_id_outside_the_library_is_rejected(forge):
    """That is a pipeline bug rather than a user choice, so it does not go through."""
    cc = forge.cc
    idea = {"b_id": "typo", "idea_text": "x", "b_domain": "?", "source_model": "m"}

    assert cc.filter_by_consensus([idea]) == []
    assert idea["consensus_check"]["status"] == "reject"

def test_a_complete_knowledge_file_gets_a_real_verdict(forge, tmp_path):
    bl = forge.bl
    cc = forge.cc
    (tmp_path / "agent_memory.md").write_text(
        "# 领域\n\n## 3. 常见误区 / 错误直觉\n- 误区一\n- 误区二\n\n"
        "## 5. 可行的创新切入点\n- 切入点一\n- 切入点二\n",
        encoding="utf-8",
    )
    bl.KNOWLEDGE_BASE_DIR = tmp_path

    idea = {"b_id": "agent_memory", "idea_text": "x", "b_domain": "d", "source_model": "m"}
    cc.filter_by_consensus([idea])

    assert idea["consensus_check"]["status"] == "pass"
    assert idea["consensus_check"]["passed"] is True

def test_the_template_satisfies_its_own_contract(forge):
    """A user copying TEMPLATE.md must land on a file the check can read."""
    cc = forge.cc
    template = (REPO / "knowledge_base" / "TEMPLATE.md").read_text(encoding="utf-8")
    assert cc.missing_sections(template) == []

def test_the_prompt_quotes_the_headings_the_file_actually_uses(forge, monkeypatch):
    """Naming a section the material does not contain leaves the model guessing.

    The shipped prompt asked for 【常见错误直觉（避坑）】, which appears in no
    knowledge file: knowledge_base/README.md defines the section as
    「常见误区 / 错误直觉」 and that is what the files use.
    """
    cc = forge.cc
    seen = {}
    monkeypatch.setattr(cc, "call_role",
                        lambda role, p, **k: seen.update(prompt=p) or "最终判定: 通过")

    knowledge = ("# 领域\n\n## 3. 常见误区 / 错误直觉\n- 误区一\n- 误区二\n\n"
                 "## 5. 可行的创新切入点\n- 切入点一\n")
    monkeypatch.setattr(cc, "load_knowledge", lambda name: knowledge)
    monkeypatch.setattr(cc, "get_b_by_id", lambda i: {"knowledge_md": "x.md"})

    cc.consensus_check({"b_id": "agent_memory", "idea_text": "x"})
    assert "【3. 常见误区 / 错误直觉】" in seen["prompt"]
    assert "【5. 可行的创新切入点】" in seen["prompt"]
    assert "常见错误直觉（避坑）" not in seen["prompt"]

def test_a_differently_worded_knowledge_file_still_works(forge, monkeypatch):
    """The knowledge base is written by whoever wants a domain, so wording varies."""
    cc = forge.cc
    seen = {}
    monkeypatch.setattr(cc, "call_role",
                        lambda role, p, **k: seen.update(prompt=p) or "最终判定: 通过")
    monkeypatch.setattr(
        cc, "load_knowledge",
        lambda name: "# D\n\n## Common Pitfalls\n- a\n- b\n\n## Research Opportunities\n- c\n",
    )
    monkeypatch.setattr(cc, "get_b_by_id", lambda i: {"knowledge_md": "x.md"})

    cc.consensus_check({"b_id": "agent_memory", "idea_text": "x"})
    assert "【Common Pitfalls】" in seen["prompt"]
    assert "【Research Opportunities】" in seen["prompt"]

def test_a_failed_review_call_is_unchecked_not_a_pass(forge, monkeypatch):
    """The last of the four fail-open paths in #25.

    Three of them now record unchecked. This one still returned True with the
    reason "检查调用失败，默认通过" -- a network blip or a rate limit read exactly
    like a reviewer that looked at the idea and cleared it.
    """
    cc = forge.cc
    monkeypatch.setattr(cc, "load_knowledge", lambda name: COMPLETE_KNOWLEDGE)
    monkeypatch.setattr(cc, "get_b_by_id", lambda i: {"knowledge_md": "x.md"})
    monkeypatch.setattr(cc, "call_role", lambda *a, **k: "")

    idea = {"b_id": "agent_memory", "idea_text": "x"}
    assert cc.filter_by_consensus([idea]) == [idea], "still goes to Step 3"
    assert idea["consensus_check"]["status"] == "unchecked"
    assert idea["consensus_check"]["passed"] is False

def test_a_reply_with_no_verdict_is_unchecked(forge, monkeypatch):
    """Anything that was not the word 不通过 counted as a pass, empty replies too."""
    cc = forge.cc
    monkeypatch.setattr(cc, "load_knowledge", lambda name: COMPLETE_KNOWLEDGE)
    monkeypatch.setattr(cc, "get_b_by_id", lambda i: {"knowledge_md": "x.md"})
    monkeypatch.setattr(cc, "call_role", lambda *a, **k: "这个想法很有意思。\n我需要更多信息。")

    idea = {"b_id": "agent_memory", "idea_text": "x"}
    cc.filter_by_consensus([idea])
    assert idea["consensus_check"]["status"] == "unchecked"

def test_an_explicit_rejection_still_rejects(forge, monkeypatch):
    cc = forge.cc
    monkeypatch.setattr(cc, "load_knowledge", lambda name: COMPLETE_KNOWLEDGE)
    monkeypatch.setattr(cc, "get_b_by_id", lambda i: {"knowledge_md": "x.md"})
    monkeypatch.setattr(cc, "call_role", lambda *a, **k: "最终判定（只输出一个词）: 不通过\n一句话总结理由: 撞了误区一")

    idea = {"b_id": "agent_memory", "idea_text": "x"}
    assert cc.filter_by_consensus([idea]) == []
    assert idea["consensus_check"]["status"] == "reject"


def test_a_consensus_rejection_keeps_the_full_reason(forge, monkeypatch):
    cc = forge.cc
    reason = "违反共识" * 300 + "共识理由尾部"
    monkeypatch.setattr(cc, "load_knowledge", lambda name: COMPLETE_KNOWLEDGE)
    monkeypatch.setattr(cc, "get_b_by_id", lambda i: {"knowledge_md": "x.md"})
    monkeypatch.setattr(
        cc,
        "call_role",
        lambda *a, **k: f"最终判定（只输出一个词）: 不通过\n一句话总结理由: {reason}",
    )

    idea = {"b_id": "agent_memory", "idea_text": "x"}
    cc.filter_by_consensus([idea])

    assert idea["consensus_check"]["reason"] == reason
