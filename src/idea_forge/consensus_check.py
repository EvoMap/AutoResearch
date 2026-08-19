"""
Idea 生成后的外部共识验证
在 idea 通过交叉验证后，再做一次"撞共识检查"：
- 基于领域方向的知识文档
- 用 Gemini Pro 严格检查 idea 是否违反了社区共识
- 避免"看起来合理但业内人不会认可"的方案
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from llm_client import call_role
from idea_forge.b_library import get_b_by_id, load_knowledge

# In-process sentinel for "could not check". Never persisted: `passed` stays a bool.
#
# UNCHECKED is not a rejection. A knowledge file is optional -- users upload the
# domains they want -- so "no material to check against" is a normal state, not a
# defect, and blocking those ideas would throw away work already paid for in
# Step 1 and Step 2. What the earlier code got wrong was recording them as
# `passed: True`, which made an unchecked idea indistinguishable from a cleared
# one. It forwards them and says so instead.
UNAVAILABLE = object()


# Sections the prompt compares against. Matched by keyword on heading lines, since
# no file spells them the prompt's way (prompt: 常见错误直觉（避坑）; file: 常见误区 / 错误直觉).
# The sections the consensus prompt compares an idea against. knowledge_base/README.md
# defines them as "常见误区 / 错误直觉" and "可行的创新切入点", and the shipped files
# use those headings with a number in front.
#
# Matched by keyword rather than by an exact string: the knowledge base is written by
# whoever wants a domain, so the wording varies, and a literal match would silently
# find nothing. English variants are here because two of the shipped files are
# English-language surveys.
REQUIRED_SECTIONS = {
    "错误直觉": ("错误直觉", "常见误区", "pitfall", "misconception", "wrong intuition"),
    "创新切入点": ("创新切入点", "创新点", "opening", "opportunit"),
}

# A heading with no list under it gives the reviewer nothing to compare against.
CONTENT_MARKERS = ("- ", "* ", "1.", "|", "#")


def locate_sections(knowledge: str, require_content: bool = True) -> dict:
    """Find each required section and return its real heading text and body.

    The prompt quotes these headings back to the reviewer, so it always names
    something that exists in the material it was given. Hardcoding a section name
    in the prompt is what produced the current mismatch: it asks for
    【常见错误直觉（避坑）】, which appears in no knowledge file, so the model has to
    guess where to look.
    """
    lines = knowledge.splitlines()

    def level(line: str) -> int:
        stripped = line.lstrip()
        return len(stripped) - len(stripped.lstrip("#"))

    heads = [(i, ln, level(ln)) for i, ln in enumerate(lines) if ln.lstrip().startswith("#")]

    found = {}
    for label, keywords in REQUIRED_SECTIONS.items():
        hit = next(
            ((i, ln, lv) for i, ln, lv in heads
             if any(k in ln.lower() for k in keywords)),
            None,
        )
        if hit is None:
            continue
        idx, heading, lv = hit
        # Same-or-higher level, not any heading: every file uses ### subsections,
        # so stopping at the first one would make each section look empty.
        end = next((i for i, _, other in heads if i > idx and other <= lv), len(lines))
        body = [ln.strip() for ln in lines[idx + 1:end] if ln.strip()]
        if require_content and not any(ln.startswith(CONTENT_MARKERS) for ln in body):
            continue
        found[label] = (heading.lstrip("# ").strip(), "\n".join(body))
    return found


def missing_sections(knowledge: str) -> list[str]:
    """Labels of the required sections this file does not usably provide.

    Distinguishes an absent heading from one with nothing under it: the first
    means write the section, the second means fill it in.
    """
    found = locate_sections(knowledge)
    headings = locate_sections(knowledge, require_content=False)
    gaps = []
    for label in REQUIRED_SECTIONS:
        if label in found:
            continue
        gaps.append(f"{label}(有标题无条目)" if label in headings else label)
    return gaps


def consensus_check(idea_item):
    """
    检查 idea 是否违反 B 领域社区共识
    返回: (通过?, 理由)
    """
    # 字段名兼容：早期写 "b_direction"，forge 现在写 "b_id"
    b_id = idea_item.get("b_id") or idea_item.get("b_direction", "")
    b = get_b_by_id(b_id)
    if not b:
        # A b_id outside the library is a pipeline bug, not a user choice.
        return False, f"B 库里没有 {b_id or '(空)'}，拒绝"

    knowledge = load_knowledge(b.get("knowledge_md", ""))
    if not knowledge:
        return UNAVAILABLE, f"无 {b.get('knowledge_md','')}，未做共识检查"

    sections = locate_sections(knowledge)
    gaps = [label for label in REQUIRED_SECTIONS if label not in sections]
    if gaps:
        # Without these sections the reviewer would compare against nothing.
        return UNAVAILABLE, f"{b.get('knowledge_md','')} 缺 {', '.join(gaps)}，未做共识检查"

    idea_text = idea_item.get("idea_text", "")

    prompt = f"""你是 B 领域的资深研究者。请判断以下 idea 是否违反了 B 领域的社区共识和常见误区。

【B 领域常识 & 社区共识】
{knowledge}

【待检查的 idea】
{idea_text}

【检查任务】
请逐条对照【{sections["错误直觉"][0]}】部分，判断这个 idea 是否撞了任何一条错误直觉。

重要：你不是在鼓励创新，你是在把关——即使 idea 看起来新颖，如果它违反了社区已经验证过的错误直觉，也要果断否决。

请严格输出:

撞了的错误直觉（如果有，列出）:
- ...

业内的人大概率会质疑的点:
- ...

是否违反了【{sections["创新切入点"][0]}】清单？(是/否)
理由:

最终判定（只输出一个词）: 通过 / 不通过
一句话总结理由:"""

    result = call_role("consensus_checker", prompt)
    if not result:
        return UNAVAILABLE, "共识检查调用失败，未做检查"

    # 不通过 contains 通过, so it has to be tested first. Still a substring test
    # on the model's own verdict line -- issue #26 tracks that.
    verdict_line = next(
        (line for line in reversed(result.splitlines())
         if "最终判定" in line or "verdict" in line.lower()),
        "",
    )
    if "不通过" in verdict_line:
        reason = ""
        for line in reversed(result.splitlines()):
            if "总结" in line or "理由" in line:
                reason = line.split(":", 1)[-1].strip() if ":" in line else line
                break
        return False, reason or "违反社区共识"
    if "通过" in verdict_line:
        return True, "通过共识检查"
    # A reply the parser cannot read is not a verdict. Treating it as one is how
    # the other three paths used to clear ideas nobody had reviewed.
    return UNAVAILABLE, "共识检查没有给出判定，未做检查"


def filter_by_consensus(validated_ideas):
    """Drop ideas the check rejected. Forward the rest, marked with what happened."""
    print(f"\n{'─' * 60}")
    print(f"  Stage 2.5: 社区共识检查 ({len(validated_ideas)} 个)")
    print(f"{'─' * 60}")

    passed, unchecked = [], 0
    for item in validated_ideas:
        print(f"\n  检查: [{item.get('b_domain','')}] {item.get('source_model','')}...")
        ok, reason = consensus_check(item)
        status = "unchecked" if ok is UNAVAILABLE else ("pass" if ok else "reject")
        item["consensus_check"] = {"passed": ok is True, "status": status, "reason": reason}

        if status == "reject":
            print(f"    ❌ {reason}")
            continue
        passed.append(item)
        if status == "unchecked":
            unchecked += 1
            print(f"    ⚠️  {reason}")
        else:
            print(f"    ✅ {reason}")
        time.sleep(3)

    checked = len(passed) - unchecked
    print(f"\n  通过共识检查: {checked}，未做检查: {unchecked}，"
          f"被否决: {len(validated_ideas) - len(passed)}")
    if unchecked:
        print("    未做检查的 idea 仍会进入 Step 3，consensus_check.status 记为 unchecked。")
        print("    要让它们也被检查：补上对应的 knowledge_base/*.md，格式见 TEMPLATE.md。")
    return passed
