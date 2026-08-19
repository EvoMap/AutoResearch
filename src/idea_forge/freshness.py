"""
时新性刷新模块（Freshness Refresh）
========================================
设计哲学：当 idea/plan 中提及过时模型/数据集/benchmark 时，**不要击杀好计划**，
而是从两个维度获取最新版本并就地替换：
  1) 领域方向库（b_library）—— 用户主动维护的权威最新基线
  2) arxiv 实时搜索 —— 兜底，覆盖领域方向库未及时更新的情况

输出：
  - 把识别到的过时项映射成最新替代项
  - 用 LLM 改写 idea_text，使所有引用的模型/数据/benchmark 全部为 2025+ 最新版本
"""

import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from llm_client import (call_model, call_role_with_model, configured_request_params,
                        configured_role_models)
from api_retry import retry_call
from idea_forge.b_library import get_b_by_id

# 过时项正则（粗筛）
STALE_PATTERNS = [
    r"LLaVA[-\s]?1\.[5-6]",
    r"Qwen2[-\s]?VL(?![\s\-]?[\d\.]*[2-9])",  # Qwen2-VL but not Qwen2.5-VL
    r"InternVL[-\s]?2(?!\.[5-9])",
    r"Llama[-\s]?[23](?![-\s]?[4-9])",
    r"Vicuna",
    r"GPT-3\.5",
    r"ChatGPT(?!\s*4)",
    r"CLIP\s+ViT[-\s]?L/14",
    r"LLaVA[-\s]?1\.0",
    r"MiniGPT[-\s]?4",
    r"InstructBLIP",
]
STALE_REGEX = re.compile("|".join(STALE_PATTERNS), re.IGNORECASE)


def quick_detect_stale_mentions(text):
    """正则粗筛 + 去重，返回检测到的过时项字符串列表"""
    hits = STALE_REGEX.findall(text)
    flat = []
    for h in hits:
        if isinstance(h, tuple):
            flat.extend(x.strip() for x in h if x)
        else:
            flat.append(h.strip())
    return list(dict.fromkeys(flat))  # 保序去重


def search_arxiv_latest(query, max_results=5, days_back=550):
    """
    在 arxiv 上搜索 query 相关的最新论文（默认 18 个月内）。
    返回 [{title, abstract, published, arxiv_id, url}]
    用于发现 B 库尚未收录的 2025-2026 新模型/数据集/benchmark。
    """
    try:
        import arxiv
    except ImportError:
        return []

    try:
        cutoff = datetime.now() - timedelta(days=days_back)
        client = arxiv.Client(num_retries=0)
        search = arxiv.Search(
            query=query,
            max_results=max_results * 3,
            sort_criterion=arxiv.SortCriterion.Relevance,
        )
        papers = retry_call(
            lambda: list(client.results(search)),
            operation_name=f"arXiv freshness {query}",
        )
        results = []
        for paper in papers:
            if paper.published.replace(tzinfo=None) < cutoff:
                continue
            results.append({
                "title": paper.title,
                "abstract": paper.summary,
                "published": paper.published.strftime("%Y-%m"),
                "arxiv_id": paper.entry_id.split("/")[-1],
                "url": paper.entry_id,
            })
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        print(f"  [freshness/arxiv] 搜索失败: {e}")
        return []


def build_b_library_reference(b_id):
    """从 B 库取出权威最新基线 + 数据集"""
    b = get_b_by_id(b_id)
    if not b:
        return ""
    ref = "【B 库权威最新基线（必须优先采用）】\n"
    ref += "领域: " + b.get("domain", "") + "\n"
    ref += "最新主基线: " + ", ".join(b.get("baselines", [])) + "\n"
    ref += "最新数据集/Benchmark: " + ", ".join(b.get("datasets", []))
    return ref


def build_arxiv_reference(stale_items, max_per_item=3):
    """对每个检测到的过时项，搜 arxiv 找最新替代物"""
    parts = ["【arxiv 实时搜索 - 最新替代候选】"]
    for item in stale_items:
        query = item + " 2025 OR 2026 successor OR replacement OR latest"
        hits = search_arxiv_latest(query, max_results=max_per_item)
        if hits:
            parts.append(f"\n{item} 的可能替代候选:")
            for h in hits:
                parts.append(f"  - [{h['published']}] {h['title']} ({h['abstract']})")
    return "\n".join(parts)


def refresh_idea_freshness(item, enable_arxiv=True, refresher_model=None):
    """
    对一条已通过验证的 idea 做时新性刷新：
      1) 正则粗筛过时项
      2) 取 B 库权威基线
      3) 可选 arxiv 实时搜索
      4) LLM 改写 idea_text，用最新模型/数据替换过时项

    成功返回 True 并就地修改 item['idea_text']；失败返回 False（保留原文）。
    """
    original_text = item.get("idea_text", "")
    stale = quick_detect_stale_mentions(original_text)

    # 也检查 validation 里的 D4 freshness flags
    flags = item.get("freshness_flags", [])

    # 如果没有过时迹象，跳过
    if not stale and not flags:
        return False

    if stale:
        print(f"    检测到过时迹象: {stale}{' (+D4警告)' if flags else ''}")
    else:
        print("    检测到过时迹象 (+D4警告)")

    # 构建参考材料
    b_ref = build_b_library_reference(item.get("b_id") or item.get("b_domain", ""))
    arxiv_ref = build_arxiv_reference(stale, max_per_item=3) if enable_arxiv and stale else ""

    flags_text = "\n".join(flags) if flags else "（粗筛未命中，但 D4 评审有时新性警告，见下）"

    prompt = (
        "你是科研论文打磨专家。下面这条研究方案已经通过同行评审，技术构思良好，\n"
        "但其中提及的模型/数据集/benchmark 部分**版本偏旧**，会被 ICLR/NeurIPS 2026 审稿人质疑。\n"
        "请在**完全保留原方案核心机制、故事线、方法描述**的前提下，**只升级**模型/数据/benchmark 引用。\n\n"
        "【原方案】\n" + original_text + "\n\n"
        "【检测到的过时项】\n" + flags_text + "\n\n"
        "【D4 评审员的时新性警告】\n" + "\n".join(flags[:5]) + "\n\n"
        "【任务要求】\n"
        "1. **保留**：核心 idea 一句话、机制映射、论文故事线、方法描述（这些都是好的，不要动）。\n"
        "2. **升级**：仅替换\"关键实验\"段中的\"基线 / 数据集 / benchmark / 评估方法\"为 2025-2026 最新版本。\n"
        "   - 优先采用上面 B 库给出的权威最新基线\n"
        "   - 如果 B 库缺失某类，参考 arxiv 搜索结果或你自己知道的 2025+ 模型\n"
        "   - 老模型可以保留作为\"过时对比\"消融，但不能作为主基线\n"
        "3. **删除**：明显过时的禁用项（LLaVA-1.5/1.6、Qwen2-VL、InternVL2、Llama-2、Vicuna、GPT-3.5）作为主基线。\n\n"
        + (b_ref + "\n\n" if b_ref else "")
        + (arxiv_ref + "\n\n" if arxiv_ref else "")
        + "请直接输出升级后的完整方案（不要加额外说明）："
    )

    # 没点名就按角色走：候选顺序、逐个回落、单条预算都在 call_role 里，跟其余角色同一份。
    # 原来这里先把角色的第一个候选取出来当模型名，失败了再写死回落 gpt-5.5——配置里那条
    # 候选链运行时一个都到不了，而 preflight 探的正是它（#190）。
    if refresher_model:
        print(f"    [{refresher_model}] 调用刷新...")
        refreshed = call_model(
            refresher_model, prompt,
            **{**configured_request_params("freshness_refresher"), "temperature": 0.2})
    else:
        models = configured_role_models("freshness_refresher")
        print(f"    [{'/'.join(models)}] 调用刷新...")
        # 落盘记的是真正答话的那个，不是首选：回落发生时这两个不是同一个模型。
        refreshed, refresher_model = call_role_with_model("freshness_refresher", prompt)

    if not refreshed or len(refreshed.strip()) < 100:
        print("     ❌ 刷新失败，保留原文")
        return False

    # 保留原文备份，就地更新
    item["idea_text_original"] = original_text
    item["idea_text"] = refreshed.strip()
    item["freshness_refresh"] = {
        "stale_detected": stale,
        "refresher": refresher_model,
        "b_library_used": bool(b_ref),
        "arxiv_used": bool(arxiv_ref),
        "refreshed_at": datetime.now().isoformat(),
    }
    return True


def step2_5_freshness_refresh(validated_ideas, enable_arxiv=True):
    """
    Step 2.5: 对所有通过严格验证的 idea 做时新性刷新
    返回的列表与输入同长（不会击杀任何 idea，只可能就地升级）
    """
    print("────────────────────────────────────────────────────────────")
    print(f"  Step 2.5: 时新性刷新 ({len(validated_ideas)} 个 idea)")
    print("  规则：B 库 + arxiv 双渠道；只升级模型/数据，不击杀")

    refreshed_count = 0
    for i, item in enumerate(validated_ideas):
        ok = refresh_idea_freshness(item, enable_arxiv=enable_arxiv)
        if ok:
            refreshed_count += 1
        else:
            print("    无需刷新或刷新失败，保留原文")
        time.sleep(1)

    print(f"\n  刷新完成: {refreshed_count} 个 idea 升级了模型/数据")
    return validated_ideas


if __name__ == "__main__":
    test_text = """
    草拟标题: Vision-X
    关键实验:
    - 数据集: VQAv2, GQA, 老 MMBench
    - 基线: LLaVA-1.6-7B, Qwen2-VL-7B, InternVL2-8B, Vicuna
    - 指标: F1
    """
    print("过时项检测:")
    print(quick_detect_stale_mentions(test_text))
    print("\narxiv 搜索 'LLaVA 2025 successor':")
    hits = search_arxiv_latest("LLaVA 2025 successor", max_results=3)
    for h in hits:
        print(f"  [{h['published']}] {h['title']}")
