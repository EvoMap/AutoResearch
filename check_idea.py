"""
check_idea.py — 用知识库审查一份 idea 是否存在事实性错误 / 观念老旧 / 与共识冲突

用法:
    # 列出可用知识库与 知识方向
    python check_idea.py --list

    # 用指定 .md 审查（idea 走命令行）
    python check_idea.py --kb agent_memory.md --idea "用遗忘曲线给 Agent 做硬性 token 删减..."

    # 用 b_library 里的 知识方向 id 审查（自动找对应 md）
    python check_idea.py --b-id agent_memory --idea-file my_idea.txt

    # idea 从 stdin
    cat my_idea.txt | python check_idea.py --b-id agent_memory --idea-file -

    # 切换模型（默认 claude-opus；可选 gemini-pro / gpt-5.5 / gemini-flash 等）
    python check_idea.py --b-id agent_memory --idea-file my_idea.txt --model gemini-pro

    # 把检查结果落盘
    python check_idea.py --b-id agent_memory --idea-file my_idea.txt --out review.md

    # idea 正文也可以直接传，但它通常有几千字，写进命令行不好读也不好改。
    # 真实例子看 data/ideas/ 下任意一份，或用 --idea-file。


"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KB_DIR = ROOT / "knowledge_base"

sys.path.insert(0, str(ROOT / "src"))

from llm_client import call_model, configured_request_params  # noqa: E402

DEFAULT_MODEL = "claude-opus"


def list_kb():
    """列出 --kb 接受的知识文件 + b_library 注册的 知识方向。

    列表和 --kb 必须给出同一个答案：--kb 拒绝时让用户「用 --list 看可用的」，
    而 --list 曾经直接 glob 目录，把随后必然被拒的项也列了出来。
    """
    from idea_forge.b_library import available_knowledge_files, get_b_library
    from idea_forge.b_library import has_knowledge, knowledge_path

    print(f"[知识库目录] {KB_DIR}")
    names = available_knowledge_files()
    if not names:
        print("  （没有可用的知识文件）")
    for name in names:
        size = knowledge_path(name).stat().st_size
        print(f"  - {name}.md  ({size} 字节)")

    print("\n[b_library 注册的 知识方向]")
    for b in get_b_library():
        tag = "✅" if has_knowledge(b) else "❌缺失"
        print(f"  - {b['id']:<22} → {b['knowledge_md']:<28} {tag}  ({b['domain']})")


def load_kb_content(kb_filename: str) -> tuple[str, str]:
    """返回 (md_content, domain_label)。domain_label 取 b_library 里同 md 的 domain，找不到就用文件名。"""
    from idea_forge.b_library import knowledge_path
    path = knowledge_path(kb_filename)
    if path is None:
        sys.exit(f"{kb_filename} 不是 knowledge_base/ 下的知识文件"
                 f"（用 --list 看可用的）")
    md = path.read_text(encoding="utf-8")

    domain = kb_filename.replace(".md", "")
    try:
        from idea_forge.b_library import get_b_library
        for b in get_b_library():
            if b.get("knowledge_md") == kb_filename:
                domain = b.get("domain", domain)
                break
    except Exception:
        pass
    return md, domain


def resolve_kb_filename(args) -> str:
    if args.kb:
        return args.kb
    if args.b_id:
        from idea_forge.b_library import get_b_by_id
        b = get_b_by_id(args.b_id)
        if not b:
            sys.exit(f"未知 知识方向 id: {args.b_id}（用 --list 查看可用项）")
        return b["knowledge_md"]
    sys.exit("必须指定 --kb 或 --b-id（或用 --list 查看可用项）")


def read_idea(args) -> str:
    if args.idea:
        return args.idea.strip()
    if args.idea_file:
        if args.idea_file == "-":
            return sys.stdin.read().strip()
        return Path(args.idea_file).read_text(encoding="utf-8").strip()
    sys.exit("必须提供 --idea 或 --idea-file")


def build_prompt(kb_md: str, domain: str, idea: str) -> str:
    # Quote the heading the knowledge file actually uses. Naming a section the
    # material does not contain leaves the reviewer guessing where to look, and
    # the knowledge base is written by whoever wants a domain, so the wording varies.
    sys.path.insert(0, str(ROOT / "src"))
    from idea_forge.consensus_check import locate_sections
    found = locate_sections(kb_md)
    pitfalls = found["错误直觉"][0] if "错误直觉" in found else "常见误区 / 错误直觉"

    return f"""你是 **{domain}** 领域的资深审稿人 / 顶会 PC。下面给你一份"领域知识库"，
它整理了该领域的真实社区共识、路线之争、常见误区、当前主流基线模型与数据集。请把它当作**权威基线**。

任务：基于这份知识库，对下面的"待审 idea"做严格、面向 ICLR/NeurIPS/ICML 评审的检查，重点找以下三类问题：

# 1. 事实性错误
逐条列出 idea 中与知识库不符的**事实陈述**——例如模型规格、benchmark 性质、方法机制、训练数据来源等。每条用以下格式：
- **错误**: <idea 中原文片段>
- **正确事实**: <知识库依据，引用具体段落>
- **严重程度**: 高 / 中 / 低（高=会被审稿人秒拒；中=需要修正；低=措辞不精确）

# 2. 观念老旧
逐条列出 idea 中**已被领域淘汰 / 被替代**的观点、基线、技术路线。每条用以下格式：
- **老旧之处**: <idea 中原文>
- **当前共识**: <知识库或常识里更新的版本>
- **建议替换为**: <具体的新基线 / 新方法>

# 3. 与社区共识冲突
逐条列出 idea 撞到知识库「{pitfalls}」的部分。每条用以下格式：
- **冲突点**: <idea 中原文>
- **知识库依据**: <引用 md 中的具体段落>
- **是否致命**: 致命（直接放弃此 idea）/ 可救（需大幅修改）/ 边缘（讨论后可保留）

# 4. 整体诊断
- **创新性 (1-10)**: <分数> — <一句理由>
- **可行性 (1-10)**: <分数> — <一句理由>
- **与领域共识吻合度 (1-10)**: <分数> — <一句理由>
- **一句话结论**: 强烈推荐 / 可行但需修正 / 不推荐 — <说明>

# 5. 最关键的修改建议（top 3）
按优先级列出 3 条最值得修改的点，每条 1-2 句。

---

【领域知识库（权威基线）】
{kb_md}

---

【待审 idea】
{idea}
"""


def main():
    ap = argparse.ArgumentParser(description="基于知识库审查 idea 的事实性 / 时新性 / 共识冲突")
    ap.add_argument("--list", action="store_true", help="列出可用知识库与 知识方向，然后退出")
    ap.add_argument("--kb", help="知识库 md 文件名（位于 knowledge_base/ 下），如 agent_memory.md")
    ap.add_argument("--b-id", help="知识方向 id（自动找对应 md），如 agent_memory")
    ap.add_argument("--idea", help="idea 文本（命令行内联）")
    ap.add_argument("--idea-file", help="idea 文本文件路径，用 - 表示 stdin")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"审查模型，默认 {DEFAULT_MODEL}"
                         "（可选 gemini-pro / gpt-5.5 / gemini-flash / claude-sonnet 等）")
    ap.add_argument("--max-tokens", type=int, help="临时覆盖 JSON 中的最大输出 tokens")
    ap.add_argument("--out", help="把审查结果写到指定文件（除了打印到屏幕外）")
    ap.add_argument("--temperature", type=float, default=0.2, help="采样温度，默认 0.2（审稿任务要稳）")
    args = ap.parse_args()

    if args.list:
        list_kb()
        return

    kb_filename = resolve_kb_filename(args)
    kb_md, domain = load_kb_content(kb_filename)
    idea = read_idea(args)

    print(f"[知识库] {kb_filename}  ({len(kb_md)} 字符)")
    print(f"[领域]   {domain}")
    print(f"[模型]   {args.model}")
    print(f"[idea]   {len(idea)} 字符\n")

    prompt = build_prompt(kb_md, domain, idea)

    max_tokens = args.max_tokens or configured_request_params().get("max_tokens")
    print(f"调用 {args.model} 中（max_tokens={max_tokens}, temperature={args.temperature}）...\n")
    request = {"temperature": args.temperature}
    if args.max_tokens is not None:
        request["max_tokens"] = args.max_tokens
    review = call_model(
        args.model,
        prompt,
        **request,
    )

    if not review:
        print("\n[ERROR] 模型未返回内容（key 失效 / 网络 / 配额）")
        sys.exit(1)

    print("=" * 80)
    print(review)
    print("=" * 80)

    if args.out:
        Path(args.out).write_text(
            f"# Idea 审查报告\n\n"
            f"- 知识库: `{kb_filename}`\n"
            f"- 领域: {domain}\n"
            f"- 审查模型: {args.model}\n\n"
            f"## 待审 idea\n\n{idea}\n\n"
            f"## 审查结果\n\n{review}\n",
            encoding="utf-8",
        )
        print(f"\n已写入 {args.out}")


if __name__ == "__main__":
    main()
