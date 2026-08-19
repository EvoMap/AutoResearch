"""
AutoResearch Pipeline v4 - 以社区真实讨论为核心

核心逻辑反转:
- 之前: 从论文库出发 → 看有没有人讨论 (大部分没有)
- 现在: 从社区讨论出发 → 找到被真实热议的工作 → LLM 判断是否有 insight

入口信号:
1. Reddit r/MachineLearning 近1个月 [Research] 高讨论帖
2. Reddit r/LocalLLaMA 近1个月技术向高讨论帖
3. Hacker News 近1个月 AI 高评论帖
4. 中文媒体最近报道中能找到对应社区讨论的

筛选标准:
- 必须有真实社区讨论（评论数 > 阈值，且评论内容是技术性的）
- 不要"又大又全的工程系统"，要有具体的 insight / 方法创新
- LLM(Pro) 最终判断是否真正有洞见
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "collectors"))
import channels
from config.settings import VERIFIED_DIR
from llm_client import call_role
from verdicts import STRONG, UNPARSED, conclusion_verdict, parse_judge_conclusion
try:
    from idea_forge.influential_people import get_boost_score
except ImportError:
    def get_boost_score(text):
        return 1.0, []


def normalize_post(item, source_type="community"):
    """将各采集器输出统一为 pipeline 内部格式: title, url, num_comments, score, summary, source, source_type
source_type: 'community' | 'academic' | 'media'"""
    title = item.get("title", "").strip()
    url = item.get("url") or item.get("reddit_url") or item.get("pdf_url") or item.get("hn_url") or ""
    url = url.strip() if url else ""
    summary = item.get("summary") or item.get("abstract") or item.get("selftext") or ""
    summary = summary.strip() if summary else ""
    source = item.get("source", "unknown")

    try:
        num_comments = int(item.get("num_comments", 0) or 0)
    except (ValueError, TypeError):
        num_comments = 0

    try:
        score = int(item.get("score", 0) or 0)
    except (ValueError, TypeError):
        score = 0

    normalized = {
        "title": title,
        "url": url,
        "num_comments": num_comments,
        "score": score,
        "summary": summary,
        "source": source,
        "source_type": source_type,
    }

    # Carry over any extra fields that may be useful downstream
    for k, v in item.items():
        if k not in normalized:
            normalized[k] = v

    return normalized


def _normalize_title(t):
    """归一化 title 用于模糊匹配：去掉常见前缀/标点/空白，统一小写，保留前 60 字符"""
    if not t:
        return ""
    # Strip common Reddit/HN prefixes
    t = re.sub(r'^\[(P|D|R|N|Q|L|Research|Discussion|Project|News)\]\s*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'^(Ask HN|Show HN|Tell HN)\s*:\s*', '', t, flags=re.IGNORECASE)
    # Remove punctuation/whitespace noise
    t = re.sub(r'[\s\-_/\(\)\[\]\.,\'":;!?]+', ' ', t)
    t = t.strip().lower()
    return t[:60]


def load_processed_keys():
    """扫描 verified/pipeline_v4_*.json，收集所有已被 final_pro_judgment 处理过的帖子的
    url / reddit_url / hn_url / title，作为黑名单。

返回 (urls_set, titles_set, normalized_titles_set)。
任何帖子只要 url、原 title、或归一化 title 命中其一就视作"已处理"。"""
    urls_set = set()
    titles_set = set()
    normalized_titles_set = set()

    VERIFIED_DIR.mkdir(parents=True, exist_ok=True)
    pattern_files = list(VERIFIED_DIR.glob("pipeline_v4_*.json"))

    for fpath in pattern_files:
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
            candidates = data.get("final_candidates", [])
            for c in candidates:
                url = c.get("url", "")
                if url:
                    urls_set.add(url)
                # Also capture reddit_url / hn_url if present
                for field in ("reddit_url", "hn_url", "pdf_url"):
                    extra_url = c.get(field, "")
                    if extra_url:
                        urls_set.add(extra_url)
                title = c.get("title", "")
                if title:
                    titles_set.add(title)
                    normalized_titles_set.add(_normalize_title(title))
        except Exception:
            continue

    return urls_set, titles_set, normalized_titles_set


def filter_already_processed(posts, processed_urls, processed_titles, processed_norm, label=""):
    """剔除历史已研判过的帖子；url 精确 + title 精确 + 归一化 title 模糊三路兜底。"""
    fresh = []
    filtered_count = 0
    for p in posts:
        url = p.get("url", "")
        title = p.get("title", "")
        norm = _normalize_title(title)

        if (url and url in processed_urls) or \
           (title and title in processed_titles) or \
           (norm and norm in processed_norm):
            filtered_count += 1
            continue
        fresh.append(p)

    if filtered_count:
        tag = f"[{label}] " if label else ""
        print(f"  {tag}历史去重: 剔除 {filtered_count} 条，剩余 {len(fresh)} 条")
    return fresh


def filter_by_discussion_quality(posts, min_comments=15, min_score=50):
    """只保留有真实讨论的帖子:
- 评论数 >= 15（有人真的在讨论）
- score >= 50（社区认可）
- 排除纯 meme / 纯问答"""
    meme_keywords = [
        "hot take", "am i wrong", "shower thought", "unpopular opinion",
        "eli5", "change my mind", "fight me", "controversial", "rant",
        "meme", "humor", "funny", "joke", "lol",
    ]
    quality = []
    for p in posts:
        num_comments = p.get("num_comments", 0) or 0
        score = p.get("score", 0) or 0
        title_lower = (p.get("title", "") or "").lower()

        if num_comments < min_comments:
            continue
        if score < min_score:
            continue
        if len(title_lower) < 10:
            continue
        if any(kw in title_lower for kw in meme_keywords):
            continue
        quality.append(p)
    return quality


def llm_insight_filter(posts, source_label="Reddit"):
    """对帖子列表做 LLM insight 过滤，返回有洞见的帖子列表。
VIP bypass: 提及大佬（boost_score > 1.5）的帖子直接通过，不走 LLM。"""
    if not posts:
        return []

    insightful = []
    batch_size = 30

    # VIP bypass pass
    remaining = []
    for p in posts:
        text = (p.get("title", "") or "") + " " + (p.get("summary", "") or "")
        boost_score, mentions = get_boost_score(text)
        if boost_score > 1.5:
            p["_vip_boost"] = boost_score
            p["_vip_mentions"] = [m["person"] for m in mentions]
            insightful.append(p)
        else:
            remaining.append(p)

    if len(insightful) > 0:
        print(f"  [{source_label}] VIP bypass: {len(insightful)} 条直接通过")

    # Batch LLM filter for non-VIP posts
    for batch_start in range(0, len(remaining), batch_size):
        batch = remaining[batch_start: batch_start + batch_size]
        numbered = "\n".join(
            f"{i+1}. {p.get('title', '')} (评论:{p.get('num_comments',0)}, score:{p.get('score',0)})"
            for i, p in enumerate(batch)
        )

        prompt = (
            f"以下是 {source_label} 上社区引发真实讨论的帖子。\n\n"
            "请判断哪些讨论的是**有 genuine insight 的研究方法/发现**，而不是：\n"
            "- 又大又全的工程系统（如[我们发布了一个新平台]）\n"
            "- 纯产品发布（如[GPT-X 发布了]）\n"
            "- 排行榜刷分（如[我们在 X benchmark 上达到了 SOTA]）\n"
            "- 纯应用展示（如[用 AI 做了 XXX]）\n"
            "- 行业新闻/八卦\n\n"
            "我要的是：\n"
            "- 提出了一个新颖的、具体的技术 insight（如[发现 attention 在某种条件下可以被简化]）\n"
            "- 对现有方法有深刻的改进思路（不是简单的 scale up）\n"
            "- 揭示了某个反直觉的现象或规律\n"
            "- 提出了一个简洁但有力的新方法\n\n"
            f"帖子列表（共 {len(batch)} 条）：\n{numbered}\n\n"
            "请返回有 genuine insight 的帖子编号，用逗号分隔（如 1,3,5）。"
            "如果全都没有洞见，返回空字符串。只返回编号，不要解释。"
        )

        try:
            resp = call_role("screener", prompt)
            if resp:
                # Parse comma-separated numbers
                numbers = re.findall(r'\d+', resp)
                for n_str in numbers:
                    idx = int(n_str) - 1
                    if 0 <= idx < len(batch):
                        insightful.append(batch[idx])
        except Exception as e:
            print(f"  [{source_label}] LLM 过滤批次 {batch_start//batch_size + 1} 失败: {e}")

        if batch_start + batch_size < len(remaining):
            time.sleep(0.5)

    return insightful


def final_pro_judgment(candidates, top_k=10):
    """对候选帖子逐一用 Pro 模型做深度研判，返回带判断结果的列表。"""
    if not candidates:
        return []

    judged = []
    unparsed_count = 0
    for i, c in enumerate(candidates[:top_k]):
        title = c.get("title", "")
        url = c.get("url", "")
        summary = c.get("summary", "") or ""
        source = c.get("source", "")
        num_comments = c.get("num_comments", 0)
        score = c.get("score", 0)

        prompt = (
            "你是一位AI研究顾问。以下是一个在社区被真实热议的研究工作（"
            f"来源: {source}，评论数: {num_comments}，score: {score}）：\n\n"
            f"标题: {title}\n"
            f"链接: {url}\n"
            f"摘要/内容: {summary if summary else '(无)'}\n\n"
            "请从以下维度评估这项工作：\n"
            "1. 核心insight: 这个工作最核心的技术洞见是什么？\n"
            "2. 社区热议原因: 为什么社区在讨论它？是真的有价值还是只是噱头？\n"
            "3. 方法简洁度: 方法是否简洁优雅，还是又大又全的工程堆砌？\n"
            "4. 领域交叉潜力: 这个 insight 能否与其他领域或方法组合产生新想法？\n"
            "5. 资源可行性: 复现/实验需要多少资源？学术组能做吗？\n"
            "6. 最终判定: 【强推荐 / 值得深入 / 暂不适合】+ 一句话理由\n\n"
            "格式:\n"
            "核心insight: ...\n"
            "社区热议原因: ...\n"
            "方法简洁度: ...\n"
            "领域交叉潜力: ...\n"
            "资源可行性: ...\n"
            "最终判定: <三选一> —— <一句话理由>\n\n"
            "最后一行必须以「最终判定:」开头（不要写成「研判结论」或其他说法），"
            "紧跟三个词之一，再用 —— 接理由。判定词只出现一次，不要给分场景的双判定。"
        )

        try:
            resp = call_role("judge", prompt)
            judgment = resp or ""
        except Exception as e:
            print(f"  [Pro判断 {i+1}/{min(len(candidates), top_k)}] 失败: {e}")
            judgment = ""

        parsed = parse_judge_conclusion(judgment)

        c_out = dict(c)
        c_out["llm_judgment"] = judgment
        # conclusion 仍写判定原文，产出和网页都直接显示它；conclusion_verdict 是下游
        # 筛选和统计读的那一档，两者分开之后没人需要再去猜一个中文句子的意思。
        c_out["conclusion"] = parsed.line
        c_out["conclusion_verdict"] = parsed.verdict
        if parsed.verdict == UNPARSED:
            c_out["conclusion_unparsed_reason"] = parsed.reason
            unparsed_count += 1
        judged.append(c_out)

        shown = parsed.line[:60] if parsed.line else f"(读不出判定: {parsed.reason})"
        print(f"  [Pro {i+1}/{min(len(candidates), top_k)}] {title[:60]} → {shown}")

    if unparsed_count:
        # 读不出和被判为不适合在下游长得一样，都不会进 Forge。这一行是唯一能把两者
        # 分开的地方，不打就等于把解析失败算进了 judge 的判断。
        print(f"  ⚠️ {unparsed_count}/{len(judged)} 条读不出判定，它们不会进入 Forge")

    return judged


def collect_all_channels(time_filter="month", time_filter_days=30, arxiv_days=7, hf_days=7):
    """遍历渠道清单，返回归一化后的 post 列表。

    清单在 `src/channels.py`，加一路只改那一份表（#164）。回溯参数保持原样：`time_filter`
    和 `time_filter_days` 各有两路在用，按渠道改名反而说不清是哪一路。
    """
    window = channels.Window(time_filter=time_filter, time_filter_days=time_filter_days,
                             arxiv_days=arxiv_days, hf_days=hf_days)
    all_posts = []

    for channel in channels.collection_channels():
        try:
            parts = channels.collect(channel, window)
        except Exception as e:
            # Reddit 和 Hacker News 之外的每一路都可以缺席：缺配置、上游超时、模块没装，
            # 流水线少一路信号照常跑完，而不是整条停下。
            if channel.required:
                raise
            print(f"  [{channel.label}] 失败: {e}")
            continue

        counts = []
        for section, items in parts.items():
            all_posts.extend([normalize_post(i, channel.kind) for i in items])
            counts.append(f"{section}={len(items)}" if section else f"{len(items)} {channel.unit}")
        print(f"  [{channel.label}] {' '.join(counts)}")

    # ── 全局去重（url + title）─────────────────────────────────
    seen_url   = set()
    seen_title = set()
    unique = []
    for p in all_posts:
        u = p.get("url", "")
        t = _normalize_title(p.get("title", ""))
        if (u and u in seen_url) or (t and t in seen_title):
            continue
        if u:
            seen_url.add(u)
        if t:
            seen_title.add(t)
        unique.append(p)

    print(f"\n  ✅ 全渠道去重后: {len(unique)} 条（原始 {len(all_posts)} 条）")
    return unique


def run_pipeline_v4(time_filter="month", time_filter_days=30, arxiv_days=7, hf_days=7, top_k=None, output_tag=None):
    """v4 主流程：多渠道采集 → 跨天去重 → insight 过滤 → Pro 研判"""

    # Reject a negative cap here rather than in each caller. ranked[:-1] silently
    # drops one candidate and the truncation line then reports a count that cannot
    # be true; AR_3MONTH_TOP_K=-1 and --top-k -1 both reach this. Zero stays legal
    # and means exactly that: no Pro judgment this run.
    if top_k is not None and top_k < 0:
        raise ValueError(
            f"top_k must be >= 0 or None, got {top_k}. A negative cap silently drops "
            "candidates instead of bounding them; use 0 for no Pro judgment."
        )
    print(f"\n{'='*60}")
    print(f"Pipeline v4 开始 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
    print(f"{'='*60}")
    print(f"参数: time_filter={time_filter}, time_filter_days={time_filter_days}, "
          f"arxiv_days={arxiv_days}, hf_days={hf_days}, "
          f"top_k={top_k if top_k is not None else '不限制'}")
    print("\n[Step 1] 多渠道采集...")

    all_posts = collect_all_channels(
        time_filter=time_filter,
        time_filter_days=time_filter_days,
        arxiv_days=arxiv_days,
        hf_days=hf_days,
    )
    total_raw = len(all_posts)

    # Split by source type
    community = [p for p in all_posts if p.get("source_type") == "community"]
    non_community = [p for p in all_posts if p.get("source_type") != "community"]

    # Count raw HN and Reddit
    hn_raw = sum(1 for p in all_posts if "hackernews" in p.get("source", ""))
    reddit_raw = sum(1 for p in all_posts if "reddit" in p.get("source", ""))

    print("\n[Step 2] 讨论质量过滤 (community: min_comments=15, min_score=50)...")
    community_quality = filter_by_discussion_quality(community, min_comments=15, min_score=50)
    # Non-community posts (academic/media) get through with a more lenient filter
    non_community_quality = filter_by_discussion_quality(non_community, min_comments=0, min_score=0)
    all_quality = community_quality + non_community_quality
    after_quality_filter = len(all_quality)
    print(f"  质量过滤后: community={len(community_quality)}, "
          f"academic/media={len(non_community_quality)}, 合计={after_quality_filter}")

    print("\n[Step 3] 跨天去重（剔除历史已研判帖子）...")
    processed_urls, processed_titles, processed_norm = load_processed_keys()
    print(f"  历史黑名单: {len(processed_urls)} URLs, {len(processed_titles)} 标题")
    all_quality = filter_already_processed(
        all_quality, processed_urls, processed_titles, processed_norm, label="全渠道"
    )

    print("\n[Step 4] LLM insight 过滤...")
    community_posts_q = [p for p in all_quality if p.get("source_type") == "community"]
    academic_posts_q  = [p for p in all_quality if p.get("source_type") != "community"]

    community_insightful = llm_insight_filter(community_posts_q, source_label="社区")
    academic_insightful  = llm_insight_filter(academic_posts_q,  source_label="学术/媒体")

    all_insightful_full = community_insightful + academic_insightful
    after_insight_filter = len(all_insightful_full)
    print(f"  insight 过滤后: community={len(community_insightful)}, "
          f"academic={len(academic_insightful)}, 合计={after_insight_filter}")

    # Pro 研判全量 insightful（不截断）
    # top_k 限制送进 Pro 研判的候选数。原本写的是 max(top_k, len(...))，取的是较大
    # 值，所以切片长度永远不小于全量，参数从未生效；注释说的「由调用方传入 top_k 控制
    # 上限」要的是 min。
    #
    # 直接改成 min 会让日常运行从全量变成默认只研判 15 条，那是行为变更而不是修 bug。
    # 所以默认改为不限制（None），显式传入数字时才截断——参数可用，默认不变。
    # Sorted in place so output["all_insightful"] carries the same order Pro sees.
    all_insightful_full.sort(
        key=lambda p: p.get("num_comments", 0) * 2 + p.get("score", 0), reverse=True
    )
    ranked = all_insightful_full
    pro_candidates = ranked if top_k is None else ranked[:top_k]
    if top_k is not None and len(ranked) > top_k:
        print(f"  候选 {len(ranked)}，预算上限 {top_k}，截断 {len(ranked) - top_k}")

    print(f"\n[Step 5] Pro 深度研判 ({len(pro_candidates)} 候选，全量)...")
    judged = final_pro_judgment(pro_candidates, top_k=len(pro_candidates))
    final_judged = len(judged)

    # Build output
    tag = output_tag if output_tag else datetime.now().strftime("%Y%m%d")
    output_file = VERIFIED_DIR / f"pipeline_v4_{tag}.json"
    VERIFIED_DIR.mkdir(parents=True, exist_ok=True)

    output = {
        "generated_at": datetime.now().isoformat(),
        "pipeline_version": "v4",
        "approach": "community-first: 从社区真实讨论出发 → LLM insight 过滤 → Pro 研判",
        "stats": {
            "total_raw": total_raw,
            "after_quality_filter": after_quality_filter,
            "after_insight_filter": after_insight_filter,
            "final_judged": final_judged,
            "hn_raw": hn_raw,
            "reddit_raw": reddit_raw,
        },
        "final_candidates": judged,
        "all_insightful": all_insightful_full,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print("Pipeline v4 完成")
    print(f"  原始: {total_raw} → 质量过滤: {after_quality_filter} → "
          f"insight: {after_insight_filter} → Pro研判: {final_judged}")
    print(f"  输出: {output_file}")
    print(f"{'='*60}\n")

    return output


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AutoResearch Pipeline v4")
    parser.add_argument("--time-filter", default="month", help="Reddit time filter (month/year/week)")
    parser.add_argument("--days", type=int, default=30, help="HN lookback days")
    parser.add_argument("--arxiv-days", type=int, default=7, help="arXiv lookback days")
    parser.add_argument("--hf-days", type=int, default=7, help="HF Papers lookback days")
    parser.add_argument("--top-k", type=int, default=None,
                        help="cap how many candidates reach Pro judgment; unset means no cap")
    parser.add_argument("--tag", default=None, help="Output file tag")
    args = parser.parse_args()

    result = run_pipeline_v4(
        time_filter=args.time_filter,
        time_filter_days=args.days,
        arxiv_days=args.arxiv_days,
        hf_days=args.hf_days,
        top_k=args.top_k,
        output_tag=args.tag,
    )

    candidates = result.get("final_candidates", [])
    strong = [c for c in candidates if conclusion_verdict(c) == STRONG]
    print(f"\n强推荐: {len(strong)} 个")
    for c in strong:
        print(f"  - {c.get('title', '')[:80]}")
        print(f"    → {c.get('conclusion', '')[:100]}")
