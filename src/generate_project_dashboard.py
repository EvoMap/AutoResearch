"""Render static project dashboards from AutoResearch state files.

Usage:
  python src/generate_project_dashboard.py <slug>
  python src/generate_project_dashboard.py --all
  python src/generate_project_dashboard.py --all --only <slug,slug>
"""

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from idea_forge import b_library
from generate_kb_dashboard import parse_doc
from idea_provenance import IdeaProvenanceError, load_project_provenance

PROJECT_ROOT = Path(__file__).parent.parent
PROJECTS_DIR = PROJECT_ROOT / "data" / "projects"
STYLESHEET = Path(__file__).parent / "templates" / "project_dashboard.css"

STEP_ORDER = [
    ("step_0_init", "0", "Initialize"),
    ("step_A_spawn", "A", "Launch agent"),
    ("step_1_plan", "1", "Plan"),
    ("step_2_code", "2", "Implement"),
    ("step_3_review", "3", "Code review"),
    ("step_3_fix", "3f", "Fix issues"),
    ("step_4_run", "4", "Run experiment"),
    ("step_5_result_analysis", "5", "Analyze results"),
    ("step_6_critic", "6", "External review"),
    ("step_Z_close", "Z", "Close"),
]

TYPE_LABEL = {
    "agent": "Agent",
    "planning": "Planning",
    "coding": "Coding",
    "review": "Review",
    "run": "Run",
    "critic": "Critic",
    "result-analysis": "Analysis",
}

STATUS_COLOR = {
    "done": "#34d399",
    "ok": "#34d399",
    "approve": "#34d399",
    "approved": "#34d399",
    "running": "#38bdf8",
    "alive": "#38bdf8",
    "active": "#38bdf8",
    "pending": "#64748b",
    "not_started": "#64748b",
    "skipped": "#475569",
    "not_needed": "#334155",
    "failed": "#f87171",
    "blocked": "#f87171",
    "dead": "#475569",
    "fix": "#fbbf24",
    "needs_revision": "#fbbf24",
}

STATUS_LABEL = {
    "done": "Done",
    "ok": "Healthy",
    "approve": "Approved",
    "approved": "Approved",
    "running": "Running",
    "alive": "Online",
    "active": "Active",
    "pending": "Pending",
    "not_started": "Not started",
    "skipped": "Skipped",
    "not_needed": "Not needed",
    "failed": "Failed",
    "blocked": "Blocked",
    "dead": "Stopped",
    "fix": "Needs fix",
    "needs_revision": "Needs revision",
}

TERMINAL_STEP_STATUSES = frozenset({"done", "skipped", "not_needed"})


class DashboardDataError(ValueError):
    pass


def esc(s):
    return html.escape(str(s if s is not None else ""))


def trunc(s, n=180):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------- parsing --


def parse_state_md(text):
    """Parse ``## section`` blocks and their ``- key: value`` entries."""
    sections = {}
    cur = None
    last_key = None
    for line in text.splitlines():
        m = re.match(r"^##\s+([\w.]+)", line)
        if m:
            cur = m.group(1)
            sections[cur] = {}
            last_key = None
            continue
        m = re.match(r"^-\s+([\w.]+):\s*(.*)$", line)
        if m and cur is not None:
            key, val = m.group(1), m.group(2).strip()
            sections[cur][key] = val
            last_key = key
            continue
        # indented sub-bullet continuing the previous key, e.g. multi-line key_findings lists
        m = re.match(r"^\s+-\s+(.*)$", line)
        if m and cur is not None and last_key is not None:
            extra = m.group(1).strip()
            sections[cur][last_key] = sections[cur][last_key] + " | " + extra if sections[cur].get(last_key) else extra
    m = re.search(r"last update:\s*([^)]+)\)", text)
    sections["_last_update"] = m.group(1).strip() if m else ""
    return sections


def parse_status_note(raw):
    """Parse either ``done`` or ``status=done result=...`` into a tuple."""
    raw = (raw or "").strip()
    if raw.startswith("status="):
        raw = raw[len("status=") :]
    m = re.match(r"([\w-]+)\s*(.*)$", raw)
    if not m:
        return raw, ""
    status, rest = m.group(1), m.group(2).strip()
    rest = re.sub(r"^result=", "", rest).strip()
    rest = rest.strip("()").strip()
    return status, rest


def parse_kv_line(raw):
    d = {}
    for k, v in re.findall(r'(\w[\w.]*)=("[^"]*"|\S+)', raw or ""):
        d[k] = v.strip('"')
    return d


def parse_plan_md(text):
    meta = {}
    m = re.search(r"^---\n(.*?)\n---", text, re.S)
    block = m.group(1) if m else text
    mm = re.search(r'hypothesis:\s*"(.*?)"\s*\n\S', block, re.S)
    if mm:
        meta["hypothesis"] = mm.group(1)
    for key in ("experiment_stage", "status"):
        mm = re.search(rf"{key}:\s*(\w+)", block)
        if mm:
            meta[key] = mm.group(1)
    mm = re.search(r"max_gpu_hours:\s*(\S+)", block)
    if mm:
        meta["max_gpu_hours"] = mm.group(1)
    mm = re.search(r"max_revisions:\s*(\S+)", block)
    if mm:
        meta["max_revisions"] = mm.group(1)
    return meta


def parse_review_md(text):
    meta = {}
    m = re.search(r"^---\n(.*?)\n---", text, re.S | re.M)
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r"(\w+):\s*(.*)$", line.strip())
            if mm:
                meta[mm.group(1)] = mm.group(2).strip()
    issues = re.findall(r"^-\s*\[([BW]\d+)\]\s*(?:<severity:(\w+)>\s*)?(.*)$", text, re.M)
    blockers = [i for i in issues if i[0].startswith("B")]
    warnings = [i for i in issues if i[0].startswith("W")]
    return meta, blockers, warnings


def parse_critic_md(text):
    meta = {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            break
        mm = re.match(r"-\s*(\w+):\s*(.*)$", s)
        if mm:
            meta[mm.group(1)] = mm.group(2).strip()
    providers = re.findall(r"^-\s*(\w+):\s*status=(\w+)\s*model=(\S+)\s*(?:error=(.*))?$", text, re.M)
    return meta, providers


def parse_gonogo(text):
    m = re.search(r"##\s*Go/No-Go Criteria\n(.*?)(?:\n##|\Z)", text, re.S)
    if not m:
        return []
    return [ln.lstrip("- ").strip() for ln in m.group(1).splitlines() if ln.strip().startswith("-")]


# ------------------------------------------------------------- data model --


def project_root(slug):
    candidate = Path(slug)
    if not slug or candidate.is_absolute() or len(candidate.parts) != 1 or candidate.name != slug:
        raise SystemExit(f"invalid project slug: {slug!r}; expected one directory name under {PROJECTS_DIR}")

    try:
        projects_root = PROJECTS_DIR.resolve(strict=True)
        root = (PROJECTS_DIR / candidate).resolve(strict=True)
    except FileNotFoundError as exc:
        raise SystemExit(f"project not found: {PROJECTS_DIR / candidate}") from exc

    if not root.is_relative_to(projects_root) or not root.is_dir():
        raise SystemExit(f"invalid project slug: {slug!r}; resolved path leaves {projects_root}")
    return root


def read_queue(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DashboardDataError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}; repair the queue before rendering"
        ) from exc
    except OSError as exc:
        raise DashboardDataError(f"{path}: cannot read workflow queue: {exc}") from exc

    if not isinstance(payload, dict):
        raise DashboardDataError(f"{path}: expected a JSON object, got {type(payload).__name__}")
    units = payload.get("units", [])
    if not isinstance(units, list) or any(not isinstance(unit, dict) for unit in units):
        raise DashboardDataError(f"{path}: 'units' must be a list of objects")
    return payload


def load_project(slug):
    root = project_root(slug)
    state_text = (root / "state.md").read_text(encoding="utf-8") if (root / "state.md").exists() else ""
    sections = parse_state_md(state_text)
    try:
        idea_provenance = load_project_provenance(root)
    except IdeaProvenanceError as exc:
        raise DashboardDataError(str(exc)) from exc
    if sections.get("project", {}).get("idea_provenance") and idea_provenance is None:
        raise DashboardDataError(
            f"{root / 'state.md'} declares idea_provenance but {root / 'idea_provenance.json'} is missing"
        )

    queue = {"units": [], "current_cycle": 0, "max_cycles": None, "cycle_status": {}}
    qpath = root / "workflow_queue.json"
    if qpath.exists():
        queue.update(read_queue(qpath))

    plan_meta = {}
    if (root / "plan.md").exists():
        plan_meta = parse_plan_md((root / "plan.md").read_text(encoding="utf-8"))

    review_meta, blockers, warnings = {}, [], []
    if (root / "review.md").exists():
        review_meta, blockers, warnings = parse_review_md((root / "review.md").read_text(encoding="utf-8"))

    critic_meta, providers = {}, []
    if (root / "critic.md").exists():
        critic_meta, providers = parse_critic_md((root / "critic.md").read_text(encoding="utf-8"))

    gonogo = []
    summary_path = root / "results" / "summary.md"
    if summary_path.exists():
        gonogo = parse_gonogo(summary_path.read_text(encoding="utf-8"))

    step_status = {}
    for key, _, _ in STEP_ORDER:
        raw = sections.get("step_status", {}).get(key)
        if raw is not None:
            step_status[key] = parse_status_note(raw)

    units = queue.get("units", [])
    cycles = {}
    for u in units:
        cycles.setdefault(u.get("cycle", 0), []).append(u)

    return dict(
        slug=slug,
        root=root,
        sections=sections,
        queue=queue,
        plan_meta=plan_meta,
        review_meta=review_meta,
        blockers=blockers,
        warnings=warnings,
        critic_meta=critic_meta,
        providers=providers,
        gonogo=gonogo,
        step_status=step_status,
        units=units,
        cycles=cycles,
        idea_provenance=idea_provenance,
    )


# ----------------------------------------------------------------- render --


def status_dot(status, note=""):
    color = STATUS_COLOR.get(status, "#64748b")
    label = STATUS_LABEL.get(status, status or "—")
    title = f"{label}{' · ' + str(note) if note else ''}"
    return f'<span class="dot" style="--c:{color}" title="{esc(title)}"></span>'


def current_step_index(step_status):
    """Return the first stage that has not reached a terminal state."""
    for i, (key, _, _) in enumerate(STEP_ORDER):
        st, _ = step_status.get(key, ("pending", ""))
        if st not in TERMINAL_STEP_STATUSES:
            return i
    return len(STEP_ORDER)


def render_stepper(p):
    step_status = p["step_status"]
    idx_current = current_step_index(step_status)

    nodes = []
    for i, (key, code, label) in enumerate(STEP_ORDER):
        st, note = step_status.get(key, ("—", ""))
        if st == "done":
            cls = "done"
        elif st in ("skipped", "not_needed"):
            cls = "skip"
        elif i == idx_current:
            cls = "current"
        else:
            cls = "pending"
        color = STATUS_COLOR.get(st, "#38bdf8" if cls == "current" else "#334155")
        nodes.append(f"""<div class="step {cls}" style="--c:{color};--i:{i}">
          <div class="step-dot">{esc(code)}</div>
          <div class="step-label">{esc(label)}</div>
        </div>""")
    return f'<div class="stepper">{"".join(nodes)}</div>'


def render_progress(p):
    step_status = p["step_status"]
    closed = step_status.get("step_Z_close", ("pending", ""))[0] == "done"

    units = p["units"]
    total = len(units) or 1
    order = ["done", "running", "pending", "skipped", "not_needed", "failed"]
    counts = {}
    for u in units:
        counts[u.get("status", "pending")] = counts.get(u.get("status", "pending"), 0) + 1
    keys = [k for k in order if k in counts] + [k for k in counts if k not in order]

    legend = []
    for k in keys:
        n = counts[k]
        color = STATUS_COLOR.get(k, "#64748b")
        legend.append(
            f'<span class="legend-item"><i style="background:{color}"></i>'
            f"{esc(STATUS_LABEL.get(k, k))} <b>{n}</b></span>"
        )

    failed_n = counts.get("failed", 0)

    if closed:
        # only state where a completion percentage is a fact, not a guess.
        bar_pct = 100
        bar_gradient, glow = (
            ("linear-gradient(90deg,#f87171,#fbbf24)", "rgba(248,113,113,.4)")
            if failed_n
            else ("linear-gradient(90deg,#34d399,#22d3ee)", "rgba(52,211,153,.4)")
        )
        headline = '<div class="progress-num" data-target="100">100<span>%</span></div>'
        sub = f"Pipeline complete &middot; {total} execution units &middot; closed"
    else:
        # workflow_queue's unit total grows as the run proceeds (new units get
        # appended live), so a unit-based percentage would silently change
        # meaning as it runs and implies a precision nobody actually has. We
        # only print a number once the pipeline has truly closed; while it's
        # running we name the current fixed-sequence stage instead of guessing
        # how far through an unknown-size backlog it is.
        idx = current_step_index(step_status)
        stage_label = STEP_ORDER[min(idx, len(STEP_ORDER) - 1)][2]
        bar_pct = round(idx / len(STEP_ORDER) * 100)
        bar_gradient = "linear-gradient(90deg,var(--accent1),var(--accent2))"
        glow = "rgba(96,165,250,.35)"
        headline = f'<div class="progress-num progress-num-stage">{esc(stage_label)}</div>'
        sub = f"Current stage &middot; {total} execution units &middot; in progress"

    return f"""
    <div class="progress-head">
      {headline}
      <div class="progress-sub">{sub}</div>
    </div>
    <div class="progress-track"><div class="progress-track-fill" style="width:{bar_pct}%">
      <div class="progress-track-fill-inner" style="--fill:{bar_gradient};--glow:{glow}"></div>
    </div></div>
    <div class="legend">{"".join(legend)}</div>
    """


def render_cycles(p):
    cycles = p["cycles"]
    if not cycles:
        return '<div class="empty">No execution units yet.</div>'
    max_cycles = p["queue"].get("max_cycles")
    blocks = []
    unit_i = 0
    for cyc in sorted(cycles.keys(), key=lambda x: int(x) if str(x).isdigit() else 0):
        us = cycles[cyc]
        done_n = sum(1 for u in us if u.get("status") == "done")
        skip_n = sum(1 for u in us if u.get("status") in ("skipped", "not_needed"))
        head_extra = f" / {max_cycles}" if max_cycles else ""
        items = []
        for u in us:
            status = u.get("status", "pending")
            typ = TYPE_LABEL.get(u.get("type", ""), u.get("type", "") or "—")
            stage = u.get("stage")
            result = trunc(u.get("result", ""), 220)
            ts = u.get("ended_at") or u.get("started_at") or ""
            ts_short = ts[:16].replace("T", " ") if ts else ""
            stage_badge = f'<span class="pill">{esc(stage)}</span>' if stage else ""
            items.append(f"""
            <div class="unit" style="--i:{unit_i}">
              {status_dot(status)}
              <div class="unit-body">
                <div class="unit-head">
                  <span class="unit-type">{esc(typ)}</span>{stage_badge}
                  <span class="unit-id">{esc(u.get("id", ""))}</span>
                  <span class="unit-ts">{esc(ts_short)}</span>
                </div>
                {f'<div class="unit-result">{esc(result)}</div>' if result else ""}
              </div>
            </div>""")
            unit_i += 1
        blocks.append(f"""
        <div class="cycle-block">
          <div class="cycle-head">
            <span class="cycle-num">{esc(cyc)}</span>
            <span class="cycle-title">Cycle {esc(cyc)}{head_extra}</span>
            <span class="cycle-stats">{done_n} done &middot; {skip_n} skipped &middot; {len(us)} units</span>
          </div>
          <div class="cycle-line">{"".join(items)}</div>
        </div>""")
    return "".join(blocks)


def render_reviewer_cards(p):
    cards = []

    rm, blockers, warnings = p["review_meta"], p["blockers"], p["warnings"]
    if rm or blockers or warnings:
        model = rm.get("model") or rm.get("reviewer") or "Unknown model"
        bcount = rm.get("blockers_count", str(len(blockers)))
        wcount = rm.get("warnings_count", str(len(warnings)))
        issue_html = ""
        for iid, sev, text in (blockers + warnings)[:6]:
            kind = "blocker" if iid.startswith("B") else "warning"
            issue_html += f"""<div class="issue {kind}">
              <span class="issue-id">{esc(iid)}</span>
              <span class="issue-text">{esc(trunc(text, 160))}</span>
            </div>"""
        if not issue_html:
            issue_html = '<div class="issue-none">No issues found</div>'
        cards.append(f"""
        <div class="card">
          <div class="card-title">Code review <span class="card-sub">Quality gate</span></div>
          <div class="card-meta">Reviewer <b>{esc(model)}</b></div>
          <div class="badge-row">
            <span class="badge {"bad" if int(bcount or 0) else "good"}">{esc(bcount)} blockers</span>
            <span class="badge {"warn" if int(wcount or 0) else "good"}">{esc(wcount)} warnings</span>
          </div>
          <div class="issue-list">{issue_html}</div>
        </div>""")

    cm, providers = p["critic_meta"], p["providers"]
    if cm or providers:
        verdict = cm.get("verdict", "Unknown")
        conf = cm.get("confidence", "")
        prov_html = ""
        for name, status, model, err in providers:
            ok = status == "ok"
            prov_html += f"""<div class="prov {"ok" if ok else "fail"}">
              <span class="prov-name">{esc(name)}</span>
              <span class="prov-model">{esc(model)}</span>
              <span class="prov-status">{"Healthy" if ok else "Failed"}</span>
            </div>"""
        stop_reason = cm.get("stop_reason", "")
        cards.append(f"""
        <div class="card">
          <div class="card-title">External review <span class="card-sub">Independent critic</span></div>
          <div class="badge-row">
            <span class="badge {"good" if verdict == "close_recommended" else "warn"}">{esc(verdict)}</span>
            {f'<span class="badge neutral">Confidence {esc(conf)}</span>' if conf else ""}
          </div>
          {f'<div class="prov-list">{prov_html}</div>' if prov_html else ""}
          {render_stop_reason(stop_reason)}
        </div>""")

    return "".join(cards) if cards else '<div class="empty">No review records yet.</div>'


def render_findings(p):
    findings = p["sections"].get("findings", {})
    key_findings = findings.get("key_findings", "")
    bullets = ""
    if key_findings and key_findings not in ("none", ""):
        items = [x.strip() for x in key_findings.split(" | ") if x.strip()]
        bullets = "".join(f"<li>{esc(it.replace('**', ''))}</li>" for it in items)
    if not bullets:
        return ""
    hyp = p["plan_meta"].get("hypothesis", "")

    def highlight_verdict(s):
        s = esc(s.replace("**", ""))
        s = re.sub(r"\bFAIL\b", '<b class="v-fail">FAIL</b>', s)
        s = re.sub(r"\bPASS\b", '<b class="v-pass">PASS</b>', s)
        return s

    gonogo_html = ""
    if p["gonogo"]:
        items = "".join(f"<li>{highlight_verdict(g)}</li>" for g in p["gonogo"])
        gonogo_html = f"<ul class='gonogo'>{items}</ul>"
    return f"""
    <section class="section findings-section">
      <div class="eyebrow">Conclusion</div>
      <div class="section-title">Research findings</div>
      {f'<div class="hypothesis">{esc(trunc(hyp, 400))}</div>' if hyp else ""}
      <ul class="findings">{bullets}</ul>
      {gonogo_html}
    </section>"""


def render_stop_reason(reason):
    if not reason or reason == "none":
        return ""
    return f'<div class="card-note">{esc(trunc(reason, 200))}</div>'


def render_knowledge_domain(p):
    provenance = p.get("idea_provenance")
    if not provenance or not provenance.get("b_id"):
        return ""
    b_id = provenance["b_id"]
    direction = b_library.get_b_by_id(b_id)
    if direction is None:
        raise DashboardDataError(f"idea_provenance.json: unknown B direction {b_id!r}")
    path = b_library.knowledge_path(direction.get("knowledge_md", ""))
    if path is None:
        raise DashboardDataError(
            f"idea_provenance.json: knowledge document for {b_id!r} is missing or leaves knowledge_base"
        )
    try:
        document = parse_doc(path)
    except OSError as exc:
        raise DashboardDataError(f"{path}: cannot read knowledge document: {exc}") from exc
    return f"""
    <section class="section">
      <div class="eyebrow">Knowledge source</div>
      <div class="section-title">{esc(document["title"])}</div>
      <div class="card">
        <div class="card-meta">{esc(b_id)} · {esc(document["file"])}</div>
        <p class="hypothesis">{esc(document["preview"])}</p>
        <div class="card-meta">
          <span>{document["size_kb"]} KB</span> &middot; <span>{document["lines"]} lines</span> &middot;
          <span>{document["sections"]} sections</span> &middot; <span>{document["links"]} sources</span>
        </div>
        <div class="card-note"><a href="../../../knowledge_base/{esc(document["file"])}">Open source note</a></div>
      </div>
    </section>"""


def stylesheet():
    return STYLESHEET.read_text(encoding="utf-8")


def render_experiment_stage(p):
    stage = p["plan_meta"].get("experiment_stage", "")
    return f'<div class="eyebrow">{esc(stage)} experiment</div>' if stage else ""


def render_project_html(p):
    idea = p["sections"].get("project", {}).get("idea", p["slug"])
    root_path = p["sections"].get("project", {}).get("project_root", str(p["root"]))
    last_action = p["sections"].get("project", {}).get("last_action", "")
    current_step = p["sections"].get("project", {}).get("current_step", "")
    last_update = p["sections"].get("_last_update", "")

    step_status = p["step_status"]
    closed = step_status.get("step_Z_close", ("pending", ""))[0] == "done"
    pill_cls = "closed" if closed else ""
    pill_text = "Closed" if closed else f"Active &middot; Step {esc(current_step) or '?'}"

    gen_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hero_note = ""
    if last_action:
        hero_note = (
            '<div class="hero-note"><span>Last event</span>'
            f"{esc(last_action)} <span>Updated</span>{esc(last_update)}</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(p["slug"])} · AutoResearch Monitor</title>
<style>{stylesheet()}</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <span class="brand-lockup">
      <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
      <span><b>AutoResearch</b><small>Project Monitor</small></span>
    </span>
    <span class="topbar-actions">
      <a href="../../../dashboard_index.html">Project overview</a>
      <a href="../../../kb_dashboard.html">Knowledge base</a>
      <span class="snapshot">Snapshot {esc(gen_ts)}</span>
    </span>
  </div>

  <section class="hero">
    <div class="hero-top">
      <div>
        {render_experiment_stage(p)}
        <h1>{esc(trunc(idea, 90))}</h1>
        <div class="path">{esc(p["slug"])} · {esc(root_path)}</div>
      </div>
      <span class="status-pill {pill_cls}">{pill_text}</span>
    </div>
    {hero_note}
  </section>

  <section class="progress-panel">
    {render_progress(p)}
  </section>

  {render_stepper(p)}

  <div class="two-col">
    {render_reviewer_cards(p)}
  </div>

  {render_knowledge_domain(p)}

  <section class="section">
    <div class="eyebrow">Execution Log</div>
    <div class="section-title">Execution cycles</div>
    {render_cycles(p)}
  </section>

  {render_findings(p)}

  <div class="footer">
    Sources: <code>state.md</code> &middot; <code>workflow_queue.json</code> &middot;
    <code>review.md</code> &middot; <code>critic.md</code> &middot; <code>plan.md</code> &middot;
    <code>idea_provenance.json</code><br>
    Regenerate: <code>python src/generate_project_dashboard.py {esc(p["slug"])}</code>
  </div>
</div>
<script>
(function(){{
  if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  var el = document.querySelector('.progress-num');
  if (!el || !el.firstChild) return;
  var target = parseInt(el.getAttribute('data-target'), 10);
  if (isNaN(target)) return;
  var start = null, dur = 1100;
  function frame(ts){{
    if (!start) start = ts;
    var t = Math.min((ts - start) / dur, 1);
    var eased = 1 - Math.pow(1 - t, 3);
    el.firstChild.nodeValue = Math.round(eased * target);
    if (t < 1) requestAnimationFrame(frame);
  }}
  requestAnimationFrame(frame);
}})();
</script>
</body>
</html>"""


def summarize_for_index(p):
    idea = p["sections"].get("project", {}).get("idea", p["slug"])
    closed = p["step_status"].get("step_Z_close", ("pending", ""))[0] == "done"
    current_step = p["sections"].get("project", {}).get("current_step", "")
    last_update = p["sections"].get("_last_update", "")
    stage_index = current_step_index(p["step_status"])
    stage_label = STEP_ORDER[min(stage_index, len(STEP_ORDER) - 1)][2]
    stage_position = 100 if closed else round(stage_index / len(STEP_ORDER) * 100)
    return dict(
        slug=p["slug"],
        idea=idea,
        pct=100 if closed else None,
        closed=closed,
        current_step=current_step,
        stage_label=stage_label,
        stage_position=stage_position,
        last_update=last_update,
    )


def render_index_html(summaries):
    cards = []
    for i, s in enumerate(summaries):
        pill_cls = "closed" if s["closed"] else ""
        pill_text = "Closed" if s["closed"] else f"Step {esc(s['current_step']) or '?'}"
        progress_text = (
            '<span class="idx-pct">100%</span>'
            if s["closed"]
            else (f'<span class="idx-stage">{esc(s["stage_label"])}</span>')
        )
        fill_grad = (
            "linear-gradient(90deg,#34d399,#22d3ee)"
            if s["closed"]
            else "linear-gradient(90deg,var(--accent1),var(--accent2))"
        )
        cards.append(f"""
        <a class="idx-card" href="data/projects/{esc(s["slug"])}/dashboard.html" style="--i:{i}">
          <div class="idx-top">
            <span class="status-pill {pill_cls}">{pill_text}</span>
            {progress_text}
          </div>
          <div class="idx-title">{esc(trunc(s["idea"], 70))}</div>
          <div class="idx-bar"><div class="idx-fill" style="width:{s["stage_position"]}%">
            <div class="idx-fill-inner" style="--fill:{fill_grad}"></div>
          </div></div>
          <div class="idx-meta">{esc(s["slug"])} &middot; Updated {esc(s["last_update"])}</div>
        </a>""")
    gen_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AutoResearch · Project Monitor</title>
<style>{stylesheet()}</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <span class="brand-lockup">
      <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
      <span><b>AutoResearch</b><small>Project Monitor</small></span>
    </span>
    <span class="topbar-actions">
      <a href="kb_dashboard.html">Knowledge base</a>
      <span class="snapshot">Snapshot {esc(gen_ts)}</span>
    </span>
  </div>
  <section class="hero">
    <h1>Research Pipeline Overview</h1>
    <div class="hero-note">{len(summaries)} projects &middot; Select a project to inspect its full run</div>
  </section>
  <div class="idx-grid">{"".join(cards)}</div>
  <div class="footer">Regenerate: <code>python src/generate_project_dashboard.py --all</code></div>
</div>
</body>
</html>"""


def render_one(slug):
    p = load_project(slug)
    out = p["root"] / "dashboard.html"
    html_page = render_project_html(p)
    out.write_text(html_page, encoding="utf-8")
    print(f"Generated {out} ({len(html_page)} bytes)")
    return p


def render_all(only=None):
    try:
        entries = list(PROJECTS_DIR.iterdir())
    except FileNotFoundError:
        entries = []
    slugs = sorted(d.name for d in entries if d.is_dir() and (d / "state.md").exists())
    selected = set(only) if only is not None else None
    unknown = sorted(selected - set(slugs)) if selected is not None else []
    if unknown:
        available = ", ".join(slugs) or "none"
        raise SystemExit(f"unknown project slug(s): {', '.join(unknown)}; available projects: {available}")

    summaries = []
    for slug in slugs:
        p = render_one(slug)
        if selected is None or slug in selected:
            summaries.append(summarize_for_index(p))
    out = PROJECT_ROOT / "dashboard_index.html"
    out.write_text(render_index_html(summaries), encoding="utf-8")
    print(f"Generated {out} ({len(summaries)} projects)")


def parse_only(value):
    if value is None:
        return None
    selected = {slug.strip() for slug in value.split(",") if slug.strip()}
    if not selected:
        raise SystemExit("--only requires at least one project slug")
    return selected


def main():
    ap = argparse.ArgumentParser(description="Render AutoResearch project dashboards")
    ap.add_argument("slug", nargs="?", help="Project slug, for example causal_sink_pruning")
    ap.add_argument("--all", action="store_true", help="Render every project and the overview")
    ap.add_argument("--only", metavar="SLUG,...", help="Include only these projects in the overview")
    args = ap.parse_args()
    if args.only and not args.all:
        ap.error("--only requires --all")
    try:
        if args.all:
            render_all(parse_only(args.only))
        elif args.slug:
            render_one(args.slug)
        else:
            ap.print_help()
    except DashboardDataError as exc:
        print(f"dashboard error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
