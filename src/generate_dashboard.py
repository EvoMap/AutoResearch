"""
生成大浪淘沙主页 index.html（按日期 timeline 展示每天的种子）
"""

import json
import glob
import html
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import channels
from verdicts import SKIP, STRONG, WORTH, conclusion_verdict

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
VERIFIED_DIR = DATA_DIR / "verified"
CANDIDATES_DIR = DATA_DIR / "candidates"


def esc(s):
    return html.escape(str(s))


def render_sources():
    """页头那行信号源。渠道清单在 `src/channels.py`，这里只负责排版（#164）。"""
    return " · ".join(
        f"{esc(label)}: {esc(' / '.join(c.title() for c in members))}"
        for label, members in channels.by_kind()
    )


def collect_seeds_by_date():
    """按日期归集 pipeline_v4 的研判结果（含3个月批次文件）"""
    by_date = defaultdict(list)
    seen_titles_by_date = defaultdict(set)  # 同一日期内去重

    for f in sorted(glob.glob(str(VERIFIED_DIR / "pipeline_v4_*.json"))):
        stem = Path(f).stem  # e.g. pipeline_v4_20260518 or pipeline_v4_3month_20260518_1520
        parts = stem.split("_")
        # 找第一个8位数字段作为日期
        date = None
        for p in parts:
            if len(p) == 8 and p.isdigit():
                date = p
                break
        if not date:
            continue
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in d.get("final_candidates", []):
            title = (c.get("title") or "").strip()
            if title and title not in seen_titles_by_date[date]:
                seen_titles_by_date[date].add(title)
                by_date[date].append(c)

    # 降序日期
    return [(date, by_date[date]) for date in sorted(by_date.keys(), reverse=True)]


def render_seed_card(idx, c):
    title = esc((c.get("title") or "")[:140])
    url = esc(c.get("reddit_url") or c.get("hn_url") or c.get("url") or "#")
    channel = esc(c.get("subreddit") or c.get("source") or c.get("channel") or "")
    comments = c.get("num_comments") or 0
    score = c.get("score") or 0
    judgment = (c.get("llm_judgment") or "").replace("**", "")

    tag_class, tag_text = {
        STRONG: ("tag-strong", "强推荐研究信号"),
        WORTH: ("tag-worth", "值得深入"),
        SKIP: ("tag-skip", "暂不适合生成 Idea"),
    }.get(conclusion_verdict(c), ("tag-skip", "判定读不出"))

    # judgment 高亮关键词
    jhtml = ""
    if judgment and judgment != "调用失败":
        for line in judgment.split("\n"):
            line = line.strip()
            if not line:
                continue
            line_h = esc(line)
            for lab in ["核心insight", "核心 insight", "社区热议原因", "方法简洁度",
                       "领域交叉潜力", "可行性", "最终判定"]:
                if lab in line:
                    line_h = line_h.replace(lab, f'<span class="jl">{lab}</span>', 1)
                    break
            jhtml += f'<div>{line_h}</div>'

    meta_parts = []
    if channel:
        meta_parts.append(f'<span class="ch">{channel}</span>')
    if comments:
        meta_parts.append(f'<span class="hot">{comments} 评论</span>')
    if score:
        meta_parts.append(f'<span>{score} 分</span>')

    return f'''<div class="card">
<div class="card-top"><div class="rank">{idx}</div>
<h3><a href="{url}" target="_blank">{title}</a></h3>
<span class="tag {tag_class}">{tag_text}</span></div>
<div class="meta">{" ".join(meta_parts)}</div>
<div class="judgment">{jhtml}</div>
</div>'''


def generate_html():
    timeline = collect_seeds_by_date()
    if not timeline:
        print("No data found")
        return

    # 计算总览统计
    all_unique_titles = set()
    total_strong = 0
    total_worth = 0
    total_skip = 0
    total_records = 0
    for date, seeds in timeline:
        for c in seeds:
            t = (c.get("title") or "").strip()
            if t and t not in all_unique_titles:
                all_unique_titles.add(t)
                verdict = conclusion_verdict(c)
                if verdict == STRONG:
                    total_strong += 1
                elif verdict == WORTH:
                    total_worth += 1
                elif verdict == SKIP:
                    total_skip += 1
            total_records += 1

    # 渲染 timeline
    timeline_html_parts = []
    for date, seeds in timeline:
        d_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        strong_count = sum(1 for s in seeds if conclusion_verdict(s) == STRONG)

        timeline_html_parts.append('<div class="day-block">')
        timeline_html_parts.append('<div class="day-header">')
        timeline_html_parts.append(f'<h3>📅 {d_fmt}</h3>')
        timeline_html_parts.append('<div class="day-stats">')
        timeline_html_parts.append(f'<span class="badge badge-info">{len(seeds)} 候选</span> ')
        if strong_count:
            timeline_html_parts.append(f'<span class="badge badge-strong">{strong_count} 强推荐</span>')
        timeline_html_parts.append('</div></div>')

        # 按 strong → worth → skip 排序
        def k(c):
            return {STRONG: 0, WORTH: 1, SKIP: 2}.get(conclusion_verdict(c), 3)
        seeds_sorted = sorted(seeds, key=k)
        for i, c in enumerate(seeds_sorted, 1):
            timeline_html_parts.append(render_seed_card(i, c))

        timeline_html_parts.append('</div>')

    timeline_html = "\n".join(timeline_html_parts)

    # CSS 抽到 src/templates/dashboard.css：留在 f-string 里的时候，每个花括号都要双写，
    # 而且那几行长度远超行宽，是 ruff 整文件豁免 E501 的唯一原因（#31）。
    css = (Path(__file__).parent / "templates" / "dashboard.css").read_text(encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    ideas_link = '<a href="./ideas.html">→ Idea Forge Timeline</a>'
    sources = render_sources()
    html_page = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>大浪淘沙 - 每日研究热点</title>
<style>
{css}</style>
</head>
<body><div class="container">
<header>
<div>
<h1>🌊 大浪淘沙 · 每日研究热点</h1>
<div class="sub">{stamp} (UTC+8) · 累计 {len(timeline)} 个采集日 · {ideas_link}</div>
</div>
</header>

<div class="info">
<strong>信号源:</strong> {sources}<br>
<strong>筛选漏斗:</strong> 跨天去重 → 规则初筛 → LLM(Gemini Flash) insight 过滤 → LLM(Gemini Pro) 领域交叉研判
</div>

<div class="stats">
<div class="stat"><div class="n">{len(timeline)}</div><div class="l">采集日数</div></div>
<div class="stat"><div class="n">{len(all_unique_titles)}</div><div class="l">候选(去重)</div></div>
<div class="stat"><div class="n">{total_strong}</div><div class="l">强推荐研究信号</div></div>
<div class="stat"><div class="n">{total_worth}</div><div class="l">值得深入</div></div>
<div class="stat"><div class="n">{total_skip}</div><div class="l">暂不适合</div></div>
<div class="stat"><div class="n">{total_records}</div><div class="l">研判记录</div></div>
</div>

{timeline_html}

</div></body></html>'''

    output_file = PROJECT_ROOT / "index.html"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_page)
    print(f"Generated {output_file} ({len(html_page)} bytes)")


if __name__ == "__main__":
    generate_html()
