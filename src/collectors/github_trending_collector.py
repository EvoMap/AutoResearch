"""GitHub Trending 采集器 - 白色，HTML 解析"""

from bs4 import BeautifulSoup
import json
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from api_retry import http_get  # noqa: E402
from config.settings import CANDIDATES_DIR

AI_KEYWORDS = [
    "llm", "gpt", "transformer", "diffusion", "attention",
    "multimodal", "agent", "rag", "fine-tune", "finetune",
    "reasoning", "inference", "neural", "deep-learning",
    "machine-learning", "ai", "nlp", "cv", "reinforcement",
    "world-model", "embodied", "language-model",
]


def collect_trending(since="daily"):
    """采集 GitHub Trending ML/AI 相关项目"""
    repos = []
    url = f"https://github.com/trending?since={since}"
    headers = {"User-Agent": "Mozilla/5.0 AutoResearch/0.1"}

    try:
        resp = http_get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"  [GitHub] HTTP {resp.status_code}")
            return repos

        soup = BeautifulSoup(resp.text, "lxml")
        articles = soup.find_all("article", class_="Box-row")

        for article in articles:
            h2 = article.find("h2")
            if not h2:
                continue
            a_tag = h2.find("a")
            if not a_tag:
                continue

            repo_path = a_tag.get("href", "").strip("/")
            repo_name = repo_path.split("/")[-1].lower() if "/" in repo_path else ""
            description_p = article.find("p")
            description = description_p.get_text(strip=True) if description_p else ""

            stars_span = article.find("span", class_="d-inline-block float-sm-right")
            stars_today = ""
            if stars_span:
                stars_today = stars_span.get_text(strip=True)

            full_text = (repo_name + " " + description).lower()
            is_ai = any(kw in full_text for kw in AI_KEYWORDS)
            if not is_ai:
                continue

            repos.append({
                "source": "github_trending",
                "source_category": "engineering",
                "repo": repo_path,
                "url": f"https://github.com/{repo_path}",
                "description": description,
                "stars_today": stars_today,
                "since": since,
                "collected_at": datetime.now().isoformat(),
            })

        print(f"  [GitHub] Trending ({since}): 扫描 {len(articles)} 项目，AI 相关 {len(repos)} 个")
    except Exception as e:
        print(f"  [GitHub] 失败: {e}")

    return repos


def save_results(repos):
    output_file = CANDIDATES_DIR / f"github_trending_{datetime.now().strftime('%Y%m%d')}.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(repos, f, ensure_ascii=False, indent=2)
    print(f"  [GitHub] 保存 {len(repos)} 个项目到 {output_file}")
    return output_file


if __name__ == "__main__":
    print("=" * 60)
    print("GitHub Trending 采集器测试")
    print("=" * 60)
    repos = collect_trending("daily")
    if repos:
        save_results(repos)
        print("\n  AI 热门项目:")
        for r in repos[:5]:
            print(f"    {r['repo']}: {r['description'][:50]}... ({r['stars_today']})")
    else:
        print("  当前 trending 未找到 AI 相关项目（或解析问题）")
