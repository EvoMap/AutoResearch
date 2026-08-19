"""RSS 源采集器 - 白色，覆盖量子位/Leiphone/MarkTechPost/VentureBeat等"""

import feedparser
import json
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from api_retry import http_get  # noqa: E402
from config.settings import RSS_FEEDS, CANDIDATES_DIR


def collect_from_feed(name, url):
    """从单个RSS源采集"""
    articles = []
    try:
        response = http_get(url, timeout=30, follow_redirects=True)
        if response.status_code != 200:
            return [], f"HTTP {response.status_code}"
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            return [], f"解析失败: {feed.bozo_exception}"

        for entry in feed.entries[:20]:
            published = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6]).isoformat()

            articles.append({
                "source": f"rss_{name}",
                "source_category": "chinese_media" if name in ["qbitai", "leiphone_ai"] else "english_media",
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "published": published,
                "summary": entry.get("summary", ""),
                "collected_at": datetime.now().isoformat(),
            })
        return articles, None
    except Exception as e:
        return [], str(e)


def collect_all_feeds():
    """采集所有配置的RSS源"""
    all_articles = []
    results_summary = {}

    for name, url in RSS_FEEDS.items():
        articles, error = collect_from_feed(name, url)
        if error:
            results_summary[name] = f"❌ {error}"
            print(f"  [RSS] {name}: ❌ {error}")
        else:
            results_summary[name] = f"✅ {len(articles)} 篇"
            print(f"  [RSS] {name}: ✅ {len(articles)} 篇")
            all_articles.extend(articles)

    return all_articles, results_summary


def save_results(articles):
    output_file = CANDIDATES_DIR / f"rss_{datetime.now().strftime('%Y%m%d')}.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
    print(f"  [RSS] 保存 {len(articles)} 篇到 {output_file}")
    return output_file


if __name__ == "__main__":
    print("=" * 60)
    print("RSS 源采集器测试")
    print("=" * 60)
    articles, summary = collect_all_feeds()
    if articles:
        save_results(articles)
        print(f"\n  总计: {len(articles)} 篇")
        print("\n  各源状态:")
        for name, status in summary.items():
            print(f"    {name}: {status}")
    else:
        print("  未获取到任何文章")
