"""领域方向：运行哪些研究领域，以及每个领域的知识从哪来。

`B_LIBRARY` 是注册表，不是目录的镜像。一个方向参与运行是因为它被点了名——注册在这里，
或者写进 config 的 `idea_forge.b_directions`。`knowledge_base/` 下其余文档不会被读。

注册项额外带手工维护的 `datasets` / `baselines`，freshness.py 用它们升级 idea 里过时的
模型和数据集；只被 config 点名的方向这两项为空，freshness.py 会兜底走 arxiv 搜索。出现新
SOTA 时更新注册项的这两个列表。

所有读知识文件的地方都经过 `knowledge_path()`，边界判的是解析之后的路径。
"""

from pathlib import Path

KNOWLEDGE_BASE_DIR = Path(__file__).parent.parent.parent / "knowledge_base"


# Compared casefolded: on a case-insensitive filesystem `template` opens
# TEMPLATE.md, and Path.resolve() does not restore the directory entry's real
# case, so an exact-byte comparison let the reserved names back in under another
# spelling.
NOT_A_DIRECTION = {"readme.md", "template.md"}


def _direction_name(name):
    """`agent_memory.md` and `agent_memory` are the same direction.

    Both spellings are accepted (check_idea's --kb takes a filename), so they have
    to be collapsed before any lookup or dedupe.
    """
    return name[:-3] if isinstance(name, str) and name.endswith(".md") else name


def knowledge_path(name):
    """The document `name` refers to, or None when it is not one of ours.

    Judged on the resolved path, because knowledge_base/ can hold a symlink
    pointing out of it and the text ends up in a prompt. Every reader comes
    through here.
    """
    if not name:
        return None
    filename = name if name.endswith(".md") else f"{name}.md"
    try:
        real = (KNOWLEDGE_BASE_DIR / filename).resolve(strict=True)
        root = KNOWLEDGE_BASE_DIR.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_file() or not real.is_relative_to(root):
        return None
    if real.name.casefold() in NOT_A_DIRECTION:
        return None
    return real


def load_knowledge(md_filename):
    """Return a direction's knowledge text, or "" when there is no such document."""
    path = knowledge_path(md_filename)
    return path.read_text(encoding="utf-8") if path else ""


def has_knowledge(b_direction):
    """Whether this direction's knowledge file exists and is inside the directory."""
    return knowledge_path(b_direction.get("knowledge_md", "")) is not None


def _title_of(path):
    """First H1, which is where these documents put the domain name."""
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("_", " ")


def direction_from_file(name):
    """A minimal direction for a document B_LIBRARY does not register.

    Domain comes from the H1; datasets and baselines are empty, which freshness.py
    handles by searching arxiv instead.
    """
    path = knowledge_path(name)
    if path is None:
        return None
    return {
        "id": _direction_name(name),
        "domain": _title_of(path),
        "problem": "",
        "knowledge_md": path.name,
        "datasets": [],
        "baselines": [],
    }


def available_knowledge_files():
    """Every document that could be named in the config."""
    return sorted(
        p.stem for p in KNOWLEDGE_BASE_DIR.glob("*.md") if knowledge_path(p.stem)
    )


def _as_names(value, source):
    """Normalise what a caller asked for into a list of names, or None.

    A bare string means one name -- it is iterable, so passing it through would
    ask for the directions a, g, e, n. Any other non-list raises: quietly running
    the default four looks exactly like the request having worked.
    """
    if value is None or value == [] or value == "":
        return None
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(
            f"{source} 要是名字的列表，或者单个名字，"
            f"现在是 {type(value).__name__}: {value!r}"
        )
    return [i for i in value if i] or None


def _configured_ids():
    """idea_forge.b_directions from the user's config, or None if unset."""
    try:
        from llm_client import load_config
    except ModuleNotFoundError as exc:
        if exc.name != "llm_client":
            raise
        # Running this file directly puts src/idea_forge on sys.path, not src.
        return None
    raw = (load_config().get("idea_forge") or {}).get("b_directions")
    return _as_names(raw, "config 的 idea_forge.b_directions")


def select_b_directions(explicit_ids=None):
    """Which directions this run should use, and why. Returns (selected, report).

    explicit_ids, else the config, else the registry. Each name is looked up in the
    registry first, then in knowledge_base/. A name matching neither raises rather
    than being dropped: a typo and a file that was never uploaded both otherwise
    surface hours later as "my domain produced no ideas".
    """
    source = "b_ids"
    ids = _as_names(explicit_ids, source)
    if ids is None:
        source = "config 的 idea_forge.b_directions"
        ids = _configured_ids()
    if not ids:
        return list(B_LIBRARY), f"B 库注册的 {len(B_LIBRARY)} 个方向"

    selected, unknown, seen = [], [], set()
    for raw in ids:
        i = _direction_name(raw)
        if i in seen:
            continue  # each direction costs one ideation and one review round per seed
        seen.add(i)
        found = get_b_by_id(i)
        (selected.append(found) if found else unknown.append(i))
    if unknown:
        files = available_knowledge_files()
        raise ValueError(
            f"{source} 里这些既不在领域方向库、"
            f"knowledge_base/ 下也没有同名 .md: {unknown}。"
            f"当前可用的知识文件有 {len(files)} 个，"
            f"用 `python src/idea_forge/b_library.py` 查看。"
        )
    return selected, f"{source} 指定"


def print_selection(selected, reason):
    """Say which directions are in, and whether each carries curated metadata."""
    registered = {b["id"] for b in B_LIBRARY}
    print(f"\n  领域方向: {len(selected)} 个（{reason}）")
    for b in selected:
        tag = "★" if b["id"] in registered else " "
        note = "" if b.get("datasets") else "  仅知识文件，时新性刷新走 arxiv 兜底"
        print(f"    {tag} {b['id']:<30} {b['domain'][:40]}{note}")


B_LIBRARY = [
    {
        "id": "mllm_fusion",
        "domain": "多模态大模型 - 模态融合",
        "problem": "视觉信息与文本信息的深层融合机制",
        "knowledge_md": "mllm_fusion.md",
        "datasets": ["MMMU-Pro", "MEGA-Bench", "MathVista", "HallusionBench", "MMBench-V2", "BLINK"],
        "baselines": ["Qwen2.5-VL-7B/72B", "InternVL3-8B/78B", "LLaVA-OneVision-7B",
                      "Cambrian-1-8B", "NVILA-8B", "Molmo-7B"],
    },
    {
        "id": "mllm_visual_tokens",
        "domain": "多模态大模型 - 视觉 Token 管理",
        "problem": "视觉 token 的数量、层间动态、查询相关性",
        "knowledge_md": "mllm_visual_tokens.md",
        "datasets": ["MMBench-V2", "GQA", "POPE", "OCRBench", "DocVQA", "ChartQA"],
        "baselines": ["Qwen2.5-VL", "InternVL3", "LLaVA-OneVision", "FastV-2025", "VisionZip", "PyramidDrop"],
    },
    {
        "id": "llm_reasoning",
        "domain": "LLM 推理与测试时计算",
        "problem": "如何在有限推理预算下最大化推理能力",
        "knowledge_md": "llm_reasoning.md",
        "datasets": ["MATH-500", "AIME 2024/2025", "LiveCodeBench-v6", "ARC-AGI-2", "FrontierMath", "HumanEval-V"],
        "baselines": ["DeepSeek-R1", "DeepSeek-V3.1", "Qwen3-32B", "Llama-4-Scout", "QwQ-32B", "o3-mini-style"],
    },
    {
        "id": "agent_memory",
        "domain": "LLM Agent 长期记忆",
        "problem": "Agent 在长任务中的记忆管理",
        "knowledge_md": "agent_memory.md",
        "datasets": ["LoCoMo", "LongMemEval", "RULER-128K", "InfiniteBench", "SWE-Bench-Verified"],
        "baselines": ["MemGPT-v2", "A-Mem", "Letta", "LangMem 2025", "Mem0", "RAG-2025"],
    },
]


def get_b_library():
    return B_LIBRARY


def get_b_by_id(b_id):
    name = _direction_name(b_id)
    for b in B_LIBRARY:
        if b["id"] == name:
            return b
    # Not registered: it may still be a knowledge file named in the config.
    return direction_from_file(b_id)


def format_b_context(b_direction, include_full_knowledge=True):
    """
    格式化领域方向的上下文
    include_full_knowledge=True: 包含完整的 MD 知识（长但深）
    """
    ctx = ""
    ctx += "\n领域: " + b_direction.get("domain", "")
    ctx += "\n本质问题: " + b_direction.get("problem", "")
    ctx += "\n可用数据集: " + ", ".join(b_direction.get("datasets", []))
    ctx += "\n基线方法: " + ", ".join(b_direction.get("baselines", []))

    if include_full_knowledge:
        md = load_knowledge(b_direction.get("knowledge_md", ""))
        if md:
            ctx += "\n\n【社区深度知识 - 必读！】\n" + md

    return ctx


if __name__ == "__main__":
    # What can be named in the config. The registry is only the default subset;
    # this command discovers the current directory instead of copying its size.
    import sys as _sys

    registered = {b["id"]: b for b in B_LIBRARY}
    keyword = _sys.argv[1].lower() if len(_sys.argv) > 1 else ""

    names = available_knowledge_files()
    print(f"knowledge_base/ 下有 {len(names)} 个知识文件，"
          f"其中 {len(registered)} 个已在 B 库注册（标 ★，默认启用）\n")
    for name in names:
        b = registered.get(name) or direction_from_file(name)
        if keyword and keyword not in name.lower() and keyword not in b["domain"].lower():
            continue
        star = "★" if name in registered else " "
        print(f"  {star} {name:<44} {b['domain'][:46]}")

    missing = [i for i in registered if i not in set(names)]
    if missing:
        print(f"\n  已注册但没有知识文件: {', '.join(missing)}（照常出 idea，共识检查记 unchecked）")

    print('\n选方向：在 config/providers.local.json 写 '
          '"idea_forge": {"b_directions": ["<名字>", ...]}')
    print("未注册的文件也能直接写名字；它没有 datasets/baselines，时新性刷新改走 arxiv 搜索。")
    print("按关键词筛：python src/idea_forge/b_library.py <关键词>")
