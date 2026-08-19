"""Reddit 采集器 - r/MachineLearning 与 r/LocalLLaMA 的研究讨论帖。

原来内联在 `src/pipeline_v4.py` 里，与另外 10 个采集器形态不一致，于是「加一个渠道」
没有唯一的落点，而且它没法单独测（要覆盖 run_pipeline_v4 只能把 collect_all_channels
整个 monkeypatch 掉）。搬过来只是移动，行为逐字不变（#24）。
"""

import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from api_retry import http_get  # noqa: E402
from config.settings import REDDIT_USER_AGENT  # noqa: E402  路径插入之后才可导入


REDDIT_ORIGIN = "https://www.reddit.com"


def _reddit_thread_url(permalink):
    """Build a Reddit URL without allowing a relative value to replace its host."""
    if not isinstance(permalink, str):
        return REDDIT_ORIGIN
    if (
        not permalink.startswith("/")
        or permalink.startswith("//")
        or "\\" in permalink
        or any(ord(char) < 32 for char in permalink)
    ):
        return REDDIT_ORIGIN

    candidate = f"{REDDIT_ORIGIN}{permalink}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return REDDIT_ORIGIN
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.reddit.com"
        or parsed.username is not None
        or parsed.password is not None
    ):
        return REDDIT_ORIGIN
    return candidate


def _http_url(url):
    """Return a usable HTTP(S) URL, or an empty string for unsafe schemes."""
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return url


def _is_reddit_self_url(url):
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return False
    return parsed.scheme == "https" and parsed.hostname == "www.reddit.com"


def search_reddit_research(subreddit, query, time_filter="month", limit=50):
    """在 Reddit 搜索研究相关帖子（看近1个月）"""
    url = f"https://www.reddit.com/r/{subreddit}/search.json"
    params = {
        "q": query,
        "sort": "relevance",
        "t": time_filter,
        "limit": limit,
        "restrict_sr": 1,
    }
    headers = {"User-Agent": REDDIT_USER_AGENT}
    results = []
    try:
        resp = http_get(url, params=params, headers=headers, timeout=20)
        if resp.status_code != 200:
            print(f"  [Reddit/{subreddit}] HTTP {resp.status_code}")
            return []
        data = resp.json()
        children = data.get("data", {}).get("children", [])
        for child in children:
            post = child.get("data", {})
            if not post:
                continue
            reddit_url = _reddit_thread_url(post.get("permalink", ""))
            external_url = _http_url(post.get("url", ""))
            # Prefer external link (paper) URL over reddit thread URL
            is_external = external_url and not _is_reddit_self_url(external_url)
            final_url = external_url if is_external else reddit_url
            results.append({
                "source": f"reddit_{subreddit.lower()}",
                "title": post.get("title", "").strip(),
                "url": final_url,
                "reddit_url": reddit_url,
                "score": post.get("score", 0),
                "num_comments": post.get("num_comments", 0),
                "summary": post.get("selftext") or "",
                "author": post.get("author", ""),
            })
    except Exception as e:
        print(f"  [Reddit/{subreddit}] 失败: {e}")
    return results


def get_reddit_hot_research(time_filter="month"):
    """获取近1个月内有真实技术讨论的研究帖
重点: 评论数要高（说明真的有人在讨论）"""
    all_posts = []
    seen_urls = set()
    seen_titles = set()

    # r/MachineLearning
    ml_queries = [
        "paper OR research OR method OR architecture",
        "benchmark OR evaluation OR training trick",
    ]
    for q in ml_queries:
        posts = search_reddit_research("MachineLearning", q, time_filter=time_filter)
        for p in posts:
            u = p.get("url", "")
            t = p.get("title", "")
            if (u and u in seen_urls) or (t and t in seen_titles):
                continue
            if u:
                seen_urls.add(u)
            if t:
                seen_titles.add(t)
            all_posts.append(p)

    # r/LocalLLaMA
    local_posts = search_reddit_research(
        "LocalLLaMA", "research OR paper OR method OR technique", time_filter=time_filter
    )
    for p in local_posts:
        u = p.get("url", "")
        t = p.get("title", "")
        if (u and u in seen_urls) or (t and t in seen_titles):
            continue
        if u:
            seen_urls.add(u)
        if t:
            seen_titles.add(t)
        all_posts.append(p)

    return all_posts
