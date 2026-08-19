"""
Paper Digest 采集器 - 自动论文影响力排名
"""

import feedparser
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from api_retry import http_get  # noqa: E402

PAPER_DIGEST_RSS = "https://resources.paperdigest.org/feed"


def collect_paper_digest():
    """从 Paper Digest RSS 采集高影响力论文"""
    print("\n  --- Paper Digest (影响力排名) ---")
    articles = []

    try:
        response = http_get(PAPER_DIGEST_RSS, timeout=30, follow_redirects=True)
        if response.status_code != 200:
            print(f"    ❌ HTTP {response.status_code}")
            return articles
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            print("    ❌ RSS 解析失败")
            return articles

        for entry in feed.entries[:20]:
            published = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6]).isoformat()

            articles.append({
                "source": "paper_digest",
                "source_type": "influence_ranking",
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "published": published,
                "summary": entry.get("summary", ""),
                "collected_at": datetime.now().isoformat(),
            })

        print(f"    ✅ {len(articles)} 篇")
    except Exception as e:
        print(f"    ❌ 失败: {e}")

    return articles


if __name__ == "__main__":
    print("=" * 60)
    print("  Paper Digest 采集器测试")
    print("=" * 60)
    articles = collect_paper_digest()
    for a in articles[:10]:
        print(f"  {a['title'][:60]}")
