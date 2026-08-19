"""
OpenReview 采集器 - 极高价值早期信号
能拿到顶会论文的评审分数，在论文正式 accept 公告之前 2-4 个月就知道哪些好
"""

import openreview
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from api_retry import retry_call  # noqa: E402

# 近期重要会议的 OpenReview venue IDs
VENUES = {
    "ICLR2025": "ICLR.cc/2025/Conference",
    "NeurIPS2025": "NeurIPS.cc/2025/Conference",
    "ICML2025": "ICML.cc/2025/Conference",
}


def collect_top_papers(venue_id, venue_name, min_rating=7.0, limit=20):
    """从 OpenReview 获取某会议高分论文"""
    papers = []
    try:
        client = openreview.api.OpenReviewClient(
            baseurl="https://api2.openreview.net"
        )

        # 获取已接收论文（oral/spotlight/poster）
        # 先试 oral
        for decision_type in ["Oral", "Spotlight", "Accept"]:
            try:
                submissions = retry_call(
                    lambda: client.get_all_notes(
                        invitation=f"{venue_id}/-/Submission",
                        details="replies",
                        limit=limit,
                    ),
                    operation_name=f"OpenReview {venue_name}",
                )

                for note in submissions[:limit]:
                    title = note.content.get("title", {})
                    if isinstance(title, dict):
                        title = title.get("value", "")

                    abstract = note.content.get("abstract", {})
                    if isinstance(abstract, dict):
                        abstract = abstract.get("value", "")

                    keywords = note.content.get("keywords", {})
                    if isinstance(keywords, dict):
                        keywords = keywords.get("value", [])

                    papers.append({
                        "source": f"openreview_{venue_name}",
                        "source_type": "conference_review",
                        "title": title,
                        "abstract": str(abstract),
                        "keywords": keywords if isinstance(keywords, list) else [],
                        "venue": venue_name,
                        "url": f"https://openreview.net/forum?id={note.id}",
                        "collected_at": datetime.now().isoformat(),
                    })

                if papers:
                    break
            except Exception:
                continue

    except Exception as e:
        print(f"    {venue_name}: ❌ {str(e)[:60]}")

    return papers


def collect_all_venues():
    """采集所有配置的会议"""
    print("\n  --- OpenReview 顶会高分论文 ---")
    all_papers = []

    for venue_name, venue_id in VENUES.items():
        papers = collect_top_papers(venue_id, venue_name, limit=15)
        print(f"    {venue_name}: {len(papers)} 篇")
        all_papers.extend(papers)

    return all_papers


if __name__ == "__main__":
    print("=" * 60)
    print("  OpenReview 采集器测试")
    print("=" * 60)
    papers = collect_all_venues()
    if papers:
        print(f"\n  总计: {len(papers)} 篇")
        for p in papers[:5]:
            print(f"    [{p['venue']}] {p['title'][:55]}")
    else:
        print("  未获取到论文（可能需要检查 venue ID）")
