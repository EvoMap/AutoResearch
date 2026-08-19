"""arXiv 论文采集器 - 白色，官方 API"""

import arxiv
import json
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from api_retry import retry_call  # noqa: E402
from config.settings import ARXIV_CATEGORIES, CANDIDATES_DIR


def collect_recent_papers(days_back=3, max_per_category=30):
    """采集最近N天的arXiv论文"""
    # 关闭 SDK 自己的隐式重试，由统一策略记录并控制总尝试次数。
    client = arxiv.Client(num_retries=0)
    all_papers = []

    for category in ARXIV_CATEGORIES:
        try:
            search = arxiv.Search(
                query=f"cat:{category}",
                max_results=max_per_category,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Descending,
            )
            papers = retry_call(
                lambda: list(client.results(search)),
                operation_name=f"arXiv {category}",
            )
            for paper in papers:
                all_papers.append({
                    "source": "arxiv",
                    "source_category": "academic",
                    "arxiv_id": paper.entry_id.split("/")[-1],
                    "title": paper.title,
                    "abstract": paper.summary,
                    "authors": [a.name for a in paper.authors[:5]],
                    "categories": [c for c in paper.categories],
                    "published": paper.published.isoformat(),
                    "url": paper.entry_id,
                    "pdf_url": paper.pdf_url,
                    "collected_at": datetime.now().isoformat(),
                })
            print(f"  [arXiv] {category}: 获取 {min(max_per_category, len(all_papers))} 篇")
        except Exception as e:
            print(f"  [arXiv] {category} 失败: {e}")

    return all_papers


def save_results(papers):
    """保存到候选池"""
    output_file = CANDIDATES_DIR / f"arxiv_{datetime.now().strftime('%Y%m%d')}.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)
    print(f"  [arXiv] 保存 {len(papers)} 篇到 {output_file}")
    return output_file


if __name__ == "__main__":
    print("=" * 60)
    print("arXiv 采集器测试")
    print("=" * 60)
    papers = collect_recent_papers(days_back=3, max_per_category=10)
    if papers:
        save_results(papers)
        print(f"\n  示例论文: {papers[0]['title'][:80]}...")
        print(f"  总计: {len(papers)} 篇")
    else:
        print("  未获取到论文")
