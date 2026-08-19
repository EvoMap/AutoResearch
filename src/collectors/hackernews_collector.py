"""Hacker News 采集器 - Algolia API 上近 N 天的 AI 高讨论帖。

原来内联在 `src/pipeline_v4.py` 里。同名文件曾经存在过一份无人引用的第二实现，已在
#23 删除；这一份是把真正在跑的那段搬过来，行为逐字不变（#24）。
"""

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api_retry import http_get  # noqa: E402


def get_hn_discussed(time_filter_days=30):
    """从 HN Algolia API 获取近 N 天内 AI 高讨论帖"""
    cutoff_ts = int(time.time()) - time_filter_days * 86400
    queries = [
        "LLM",
        "machine learning",
        "neural network",
        "AI research",
        "language model",
        "diffusion",
        "reasoning",
    ]
    seen_ids = set()
    results = []

    for q in queries:
        try:
            params = {
                "query": q,
                "tags": "story",
                "numericFilters": f"num_comments>20,created_at_i>{cutoff_ts}",
                "hitsPerPage": 30,
            }
            resp = http_get(
                "https://hn.algolia.com/api/v1/search",
                params=params,
                timeout=20,
            )
            if resp.status_code != 200:
                continue
            hits = resp.json().get("hits", [])
            for hit in hits:
                hn_id = hit.get("objectID", "")
                if hn_id in seen_ids:
                    continue
                seen_ids.add(hn_id)
                results.append({
                    "source": "hackernews",
                    "title": hit.get("title", "").strip(),
                    "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hn_id}",
                    "hn_url": f"https://news.ycombinator.com/item?id={hn_id}",
                    "score": hit.get("points", 0) or 0,
                    "num_comments": hit.get("num_comments", 0) or 0,
                    "summary": "",
                })
        except Exception as e:
            print(f"  [HN/{q}] 失败: {e}")
            continue

    return results
