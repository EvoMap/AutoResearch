"""
大佬声音追踪器
追踪 AI 界有影响力的人物公开支持/点赞的内容

渠道:
1. AK (@_akhaliq) - HuggingFace 每日策展
2. Karpathy - 博客/YouTube
3. 顶会 Best Paper / Oral / Spotlight
4. AI Lab 研究博客 (OpenAI, DeepMind, Anthropic, Meta AI)
5. 知名研究者个人博客 (Lilian Weng, Sebastian Raschka, Jay Alammar 等)

由于 Twitter/X 需要付费，这里用可获取的公开替代源:
- HuggingFace Papers（AK 策展）的高票子集
- 研究者博客 RSS
- 顶会官方公布
"""

import feedparser
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from api_retry import http_get  # noqa: E402
from config.settings import CANDIDATES_DIR


# ============================================================
# 1. AI 研究者博客（RSS 白色）
# ============================================================
RESEARCH_BLOGS = {
    # === 顶级个人研究者 ===
    "lilian_weng": {
        "name": "Lilian Weng (OpenAI)",
        "url": "https://lilianweng.github.io/index.xml",
        "weight": "very_high",
    },
    "sebastian_raschka": {
        "name": "Sebastian Raschka (Ahead of AI)",
        "url": "https://magazine.sebastianraschka.com/feed",
        "weight": "very_high",
    },
    "chip_huyen": {
        "name": "Chip Huyen",
        "url": "https://huyenchip.com/feed.xml",
        "weight": "high",
    },
    "jay_alammar": {
        "name": "Jay Alammar",
        "url": "http://jalammar.github.io/feed.xml",
        "weight": "high",
    },
    "colah": {
        "name": "Chris Olah (Anthropic)",
        "url": "https://colah.github.io/rss.xml",
        "weight": "very_high",
    },
    "karpathy": {
        "name": "Andrej Karpathy",
        "url": "https://karpathy.github.io/feed.xml",
        "weight": "very_high",
    },
    "simon_willison": {
        "name": "Simon Willison",
        "url": "https://simonwillison.net/atom/everything/",
        "weight": "high",
    },
    # === 顶级 Lab 博客 ===
    "openai_blog": {
        "name": "OpenAI Research",
        "url": "https://openai.com/blog/rss.xml",
        "weight": "very_high",
    },
    "deepmind_blog": {
        "name": "Google DeepMind",
        "url": "https://deepmind.google/blog/rss.xml",
        "weight": "very_high",
    },
    "google_research": {
        "name": "Google Research",
        "url": "https://blog.research.google/feeds/posts/default?alt=rss",
        "weight": "very_high",
    },
    "microsoft_research": {
        "name": "Microsoft Research",
        "url": "https://www.microsoft.com/en-us/research/feed/",
        "weight": "high",
    },
    "bair_blog": {
        "name": "BAIR (Berkeley AI Research)",
        "url": "https://bair.berkeley.edu/blog/feed.xml",
        "weight": "very_high",
    },
    "huggingface_blog": {
        "name": "Hugging Face Blog",
        "url": "https://huggingface.co/blog/feed.xml",
        "weight": "high",
    },
    # === 学术深度博客 ===
    "distill_pub": {
        "name": "Distill.pub",
        "url": "https://distill.pub/rss.xml",
        "weight": "very_high",
    },
    "offconvex": {
        "name": "Off the Convex Path",
        "url": "https://www.offconvex.org/feed.xml",
        "weight": "high",
    },
    "gradient": {
        "name": "The Gradient",
        "url": "https://thegradient.pub/rss/",
        "weight": "high",
    },
}


def collect_research_blogs(max_days=30):
    """采集研究者博客最近1个月的文章"""
    print("\n  --- AI 研究者博客 + Lab 博客 ---")
    articles = []
    cutoff = datetime.now() - timedelta(days=max_days)

    for blog_id, blog_info in RESEARCH_BLOGS.items():
        try:
            response = http_get(blog_info["url"], timeout=30, follow_redirects=True)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            feed = feedparser.parse(response.content)
            count = 0
            for entry in feed.entries[:10]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime(*entry.updated_parsed[:6])

                # 如果有日期且超出范围则跳过
                if published and published < cutoff:
                    continue

                articles.append({
                    "source": f"blog_{blog_id}",
                    "source_type": "influential_blog",
                    "blog_name": blog_info["name"],
                    "weight": blog_info["weight"],
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "published": published.isoformat() if published else "",
                    "summary": entry.get("summary", ""),
                    "collected_at": datetime.now().isoformat(),
                })
                count += 1

            status = f"✅ {count} 篇" if count > 0 else "无新文章"
            if feed.bozo and not feed.entries:
                status = "❌ 解析失败"
            print(f"    {blog_info['name']}: {status}")
        except Exception as e:
            print(f"    {blog_info['name']}: ❌ {str(e)[:40]}")

    return articles


# ============================================================
# 2. 顶会 Best Paper / Oral（从公开列表获取）
# ============================================================
# 近期顶会的 Best Paper 和 Oral 论文
# 这些通常在会议官网/OpenReview/社区整理中公开
CONFERENCE_PAPERS_URLS = {
    "iclr2025_outstanding": "https://raw.githubusercontent.com/huyenchip/ml-interviews-book/master/contents/8.1.1-papers.md",
    # 使用 HuggingFace Papers 的会议标签作为替代
}


def collect_conference_highlights():
    """
    采集顶会亮点论文
    策略: 搜索 Reddit/HN 中讨论顶会 best paper 的帖子
    """
    print("\n  --- 顶会 Best Paper / Oral ---")
    pass  # reddit search is done inline below

    conference_terms = [
        "best paper", "oral presentation", "spotlight",
        "ICLR 2025", "ICLR 2026", "ICML 2025", "ICML 2026",
        "NeurIPS 2025", "CVPR 2025", "CVPR 2026",
        "ACL 2025", "EMNLP 2025",
        "AAAI 2025", "AAAI 2026",
        "ECCV 2024", "ICCV 2025",
        "outstanding paper", "award",
    ]

    all_posts = []
    for term in conference_terms[:5]:  # 限制请求数
        url = "https://www.reddit.com/r/MachineLearning/search.json"
        params = {
            "q": term,
            "sort": "top",
            "t": "year",
            "limit": 10,
            "restrict_sr": "true",
        }
        headers = {"User-Agent": "AutoResearch/0.1"}
        try:
            resp = http_get(url, params=params, headers=headers, timeout=15, follow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                for child in data.get("data", {}).get("children", []):
                    post = child.get("data", {})
                    if post.get("score", 0) >= 30:
                        all_posts.append({
                            "source": "conference_highlight",
                            "source_type": "conference",
                            "title": post.get("title", ""),
                            "url": post.get("url", ""),
                            "reddit_url": f"https://www.reddit.com{post.get('permalink', '')}",
                            "score": post.get("score", 0),
                            "num_comments": post.get("num_comments", 0),
                            "flair": post.get("link_flair_text", ""),
                            "created": datetime.fromtimestamp(post.get("created_utc", 0)).isoformat(),
                            "collected_at": datetime.now().isoformat(),
                        })
            time.sleep(3)
        except Exception:
            pass

    # 去重
    seen = set()
    unique = []
    for p in all_posts:
        if p["title"] not in seen:
            seen.add(p["title"])
            unique.append(p)

    unique.sort(key=lambda x: x.get("score", 0), reverse=True)
    print(f"    找到 {len(unique)} 个顶会相关热议帖")
    return unique[:15]


# ============================================================
# 3. HuggingFace Papers 中 AK 策展 + 顶级作者的论文
# ============================================================
def collect_ak_curated(days_back=7):
    """
    AK 策展的 HuggingFace Papers 中，只取那些:
    - 被 AK 本人提交的（submittedBy 包含 akhaliq）
    - 或来自知名机构的
    不看 upvotes（不可靠），看提交者身份
    """
    print("\n  --- AK 策展 (HuggingFace) ---")
    sys.path.insert(0, str(Path(__file__).parent))
    from hf_papers_collector import collect_daily_papers

    all_papers = collect_daily_papers(days_back=days_back)
    ak_curated = []

    for paper in all_papers:
        submitted_by = paper.get("submitted_by", "").lower()
        # AK 本人提交的
        if "akhaliq" in submitted_by or "ak" == submitted_by:
            paper["source_type"] = "ak_curated"
            paper["curation_signal"] = "AK 本人提交"
            ak_curated.append(paper)

    print(f"    AK 本人提交: {len(ak_curated)} 篇（近{days_back}天）")
    return ak_curated


# ============================================================
# 4. 储备池机制
# ============================================================
def load_reserve_pool():
    """加载储备池"""
    reserve_file = CANDIDATES_DIR / "reserve_pool.json"
    if reserve_file.exists():
        with open(reserve_file) as f:
            return json.load(f)
    return []


def save_to_reserve_pool(items):
    """保存到储备池（追加模式）"""
    reserve_file = CANDIDATES_DIR / "reserve_pool.json"
    existing = load_reserve_pool()

    # 去重
    existing_titles = {item["title"] for item in existing}
    new_items = [item for item in items if item.get("title") not in existing_titles]

    for item in new_items:
        item["added_to_reserve"] = datetime.now().isoformat()
        item["check_again_after"] = (datetime.now() + timedelta(days=14)).isoformat()

    existing.extend(new_items)
    with open(reserve_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print(f"    储备池: +{len(new_items)} 新增, 总计 {len(existing)} 条")
    return existing


def check_reserve_matured():
    """检查储备池中哪些已经过了观察期（2周），可以重新评估"""
    reserve = load_reserve_pool()
    now = datetime.now()
    matured = []
    for item in reserve:
        check_after = item.get("check_again_after", "")
        if check_after:
            try:
                check_date = datetime.fromisoformat(check_after)
                if now >= check_date:
                    matured.append(item)
            except Exception:  # noqa: BLE001 - keep collecting on any single-source failure
                pass
    return matured


# ============================================================
# 主入口
# ============================================================
def collect_all_influential():
    """采集所有大佬/权威渠道"""
    results = {
        "blogs": [],
        "conferences": [],
        "ak_curated": [],
    }

    results["blogs"] = collect_research_blogs(max_days=30)
    results["conferences"] = collect_conference_highlights()
    results["ak_curated"] = collect_ak_curated(days_back=7)

    # 合并
    all_items = results["blogs"] + results["conferences"] + results["ak_curated"]

    # 保存
    output_file = CANDIDATES_DIR / f"influential_{datetime.now().strftime('%Y%m%d')}.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    print(f"\n  大佬/权威渠道总计: {len(all_items)} 条")
    print(f"    博客: {len(results['blogs'])}")
    print(f"    顶会: {len(results['conferences'])}")
    print(f"    AK策展: {len(results['ak_curated'])}")
    print(f"  保存到: {output_file}")

    return all_items, results


if __name__ == "__main__":
    print("=" * 60)
    print("  大佬/权威渠道采集器测试")
    print("=" * 60)
    all_items, results = collect_all_influential()

    if results["blogs"]:
        print("\n  博客文章示例:")
        for b in results["blogs"][:5]:
            print(f"    [{b['blog_name']}] {b['title'][:50]}")

    if results["conferences"]:
        print("\n  顶会热议示例:")
        for c in results["conferences"][:5]:
            print(f"    [{c['score']}↑ {c['num_comments']}评] {c['title'][:50]}")
