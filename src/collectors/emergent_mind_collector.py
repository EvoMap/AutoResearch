"""
Emergent Mind 采集器 - 聚合 Twitter/Reddit/GitHub 对 arXiv 论文的社交讨论热度
这是一个"论文 + 社交讨论"的桥梁信号，非常适合发现被热议的研究
"""

from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from api_retry import http_get  # noqa: E402

EMERGENT_MIND_RSS = "https://www.emergentmind.com/feeds/rss"


def collect_emergent_mind():
    """从 Emergent Mind 首页采集社交热度论文（HTML 解析，RSS 目前为空）"""
    print("\n  --- Emergent Mind (社交讨论热度聚合) ---")
    articles = []

    try:
        from bs4 import BeautifulSoup

        resp = http_get("https://www.emergentmind.com", timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            print(f"    ❌ HTTP {resp.status_code}")
            return articles

        soup = BeautifulSoup(resp.text, "lxml")
        paper_links = soup.find_all("a", href=True)

        for link in paper_links:
            href = link.get("href", "")
            if "/papers/" not in href:
                continue
            title = link.get_text(strip=True)
            if not title or len(title) < 10:
                continue

            arxiv_id = href.split("/papers/")[-1] if "/papers/" in href else ""
            full_url = f"https://www.emergentmind.com{href}" if href.startswith("/") else href

            articles.append({
                "source": "emergent_mind",
                "source_type": "social_aggregate",
                "title": title,
                "url": full_url,
                "arxiv_id": arxiv_id,
                "collected_at": datetime.now().isoformat(),
            })

        # 去重
        seen = set()
        unique = []
        for a in articles:
            if a["title"] not in seen:
                seen.add(a["title"])
                unique.append(a)
        articles = unique

        print(f"    ✅ {len(articles)} 篇社交热议论文")
    except Exception as e:
        print(f"    ❌ 失败: {e}")

    return articles


if __name__ == "__main__":
    print("=" * 60)
    print("  Emergent Mind 采集器测试")
    print("=" * 60)
    articles = collect_emergent_mind()
    for a in articles[:10]:
        print(f"  {a['title'][:60]}")
