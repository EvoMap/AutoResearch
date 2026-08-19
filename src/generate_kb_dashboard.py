"""Render a static board from knowledge documents accepted by the idea forge."""

import argparse
import html
import re
from datetime import datetime, timezone
from pathlib import Path

from idea_forge import b_library

PROJECT_ROOT = Path(__file__).parent.parent
BASE_STYLESHEET = Path(__file__).parent / "templates" / "project_dashboard.css"
STYLESHEET = Path(__file__).parent / "templates" / "kb_dashboard.css"

LANES = [
    (
        "Multimodal & Vision",
        (
            "3d",
            "multimodal",
            "vlm",
            "mllm",
            "vision",
            "image",
            "video",
            "visual",
            "视觉",
            "图像",
            "视频",
            "多模态",
            "跨模态",
            "文-图-音",
        ),
    ),
    ("Agents & Memory", ("agent", "memory", "context", "gui", "harness", "work_", "记忆", "上下文")),
    (
        "Reinforcement Learning",
        (
            "rl",
            "reinforcement",
            "post_training",
            "post-training",
            "强化学习",
            "opd",
            "sft",
            "reward",
            "rubric",
            "rollout",
        ),
    ),
    (
        "Reasoning & Verification",
        ("reasoning", "verifier", "verification", "test_time", "search"),
    ),
    (
        "Training Systems",
        (
            "training",
            "cluster",
            "distributed",
            "parallel",
            "kernel",
            "infrastructure",
            "集群",
            "分布式",
            "并行策略",
            "显存",
            "算力调度",
            "万卡",
            "低精度",
            "容错",
            "训练动态",
        ),
    ),
    (
        "Inference & Generation",
        (
            "inference",
            "generation",
            "kv_cache",
            "推理引擎",
            "投机解码",
            "生成加速",
            "扩散",
            "流匹配",
            "量化",
            "稀疏",
            "异构",
        ),
    ),
    ("Data Synthesis", ("data", "trajectory", "simulation", "synthetic", "数据", "轨迹", "仿真")),
    ("Evaluation & Safety", ("evaluation", "benchmark", "safety", "red_team", "评测", "评估", "红队", "judge", "stem")),
    ("World Models & Embodied AI", ("world_model", "embodied", "robot", "世界模型", "世界动作", "具身", "vla")),
    ("Audio & Speech", ("audio", "speech", "语音", "音视频")),
    (
        "Architecture & Interpretability",
        ("architecture", "interpretability", "scaling_law", "架构", "可解释", "机制机理", "预训练", "内化"),
    ),
]
LANE_OTHER = "Other"
ACCENTS = ("#72e6b8", "#72c7df", "#a99be8", "#e7c36b", "#ee8d84")


def esc(value):
    return html.escape(str(value), quote=True)


def parse_doc(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    title = next(
        (match.group(1).strip() for line in text.splitlines() if (match := re.match(r"^#\s+(.+)", line.strip()))),
        path.stem,
    )
    preview = ""
    for paragraph in re.split(r"\n\s*\n", text):
        candidate = paragraph.strip()
        if candidate and not candidate.startswith(("#", "---")):
            preview = re.sub(r"\s+", " ", candidate)
            break
    if len(preview) > 120:
        preview = preview[:119].rstrip() + "…"
    return {
        "title": title,
        "file": path.name,
        "size_kb": round(path.stat().st_size / 1024, 1),
        "lines": len(text.splitlines()),
        "sections": len(re.findall(r"^##\s", text, re.M)),
        "links": len(re.findall(r"\[[^\]]+\]\(https?://[^)]+\)", text)),
        "preview": preview,
    }


def lane_of(filename):
    folded = filename.casefold()
    for lane, keywords in LANES:
        if any(keyword.casefold() in folded for keyword in keywords):
            return lane
    return LANE_OTHER


def load_documents():
    documents = []
    for name in b_library.available_knowledge_files():
        path = b_library.knowledge_path(name)
        if path is None:
            continue
        document = parse_doc(path)
        document["lane"] = lane_of(path.name)
        documents.append(document)
    return documents


def stylesheet():
    return "\n".join(path.read_text(encoding="utf-8") for path in (BASE_STYLESHEET, STYLESHEET))


def render_document(document, index):
    return f"""
      <article class="doc" style="--i:{index}">
        <div class="doc-title">{esc(document["title"])}</div>
        <div class="doc-file">{esc(document["file"])}</div>
        <div class="doc-preview">{esc(document["preview"])}</div>
        <div class="doc-meta">
          <span>{document["size_kb"]} KB</span><span>{document["lines"]} lines</span>
          <span>{document["sections"]} sections</span><span>{document["links"]} sources</span>
        </div>
      </article>"""


def render_page(documents):
    grouped = []
    for lane, _ in [*LANES, (LANE_OTHER, ())]:
        lane_documents = [document for document in documents if document["lane"] == lane]
        if lane_documents:
            grouped.append((lane, lane_documents))

    lanes = []
    for index, (lane, lane_documents) in enumerate(grouped):
        cards = "".join(
            render_document(document, card_index)
            for card_index, document in enumerate(
                sorted(lane_documents, key=lambda item: (-item["size_kb"], item["file"]))
            )
        )
        lanes.append(f"""
    <section class="lane" style="--accent:{ACCENTS[index % len(ACCENTS)]}">
      <div class="lane-head">
        <span class="lane-dot"></span>
        <h2>{esc(lane)}</h2>
        <span class="lane-count">{len(lane_documents)}</span>
      </div>
      <div class="lane-body">{cards}
      </div>
    </section>""")

    board = "".join(lanes)
    board_class = "board" if lanes else "board empty-board"
    if not board:
        board = '<div class="empty-state">No runtime-approved knowledge documents are available.</div>'

    total_size = sum(document["size_kb"] for document in documents)
    total_links = sum(document["links"] for document in documents)
    total_sections = sum(document["sections"] for document in documents)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AutoResearch &middot; Knowledge Library</title>
<style>{stylesheet()}</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <span class="brand-lockup">
      <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
      <span><b>AutoResearch</b><small>Knowledge Library</small></span>
    </span>
    <span class="topbar-actions">
      <a href="dashboard_index.html">Project overview</a>
      <span class="snapshot">Snapshot {esc(generated)}</span>
    </span>
  </div>
  <section class="hero">
    <div class="eyebrow">Research knowledge</div>
    <h1>Knowledge Library</h1>
    <div class="hero-note">
      {len(documents)} research documents across {len(grouped)} domains &middot; runtime-approved sources only
    </div>
  </section>
  <div class="stats">
    <div class="stat"><div class="stat-value">{len(documents)}</div><div class="stat-label">Documents</div></div>
    <div class="stat"><div class="stat-value">{len(grouped)}</div><div class="stat-label">Domains</div></div>
    <div class="stat"><div class="stat-value">{total_size:.0f} KB</div><div class="stat-label">Library size</div></div>
    <div class="stat"><div class="stat-value">{total_sections}</div><div class="stat-label">Sections</div></div>
    <div class="stat"><div class="stat-value">{total_links}</div><div class="stat-label">References</div></div>
  </div>
  <main class="{board_class}">{board}
  </main>
  <div class="footer">
    Regenerate: <code>python src/generate_kb_dashboard.py</code> &middot; Companion: <code>dashboard_index.html</code>
  </div>
</div>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Render the AutoResearch knowledge library")
    parser.add_argument("-o", "--output", default=str(PROJECT_ROOT / "kb_dashboard.html"))
    args = parser.parse_args()
    page = render_page(load_documents())
    output = Path(args.output)
    output.write_text(page, encoding="utf-8")
    print(f"Generated {output} ({len(page)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
