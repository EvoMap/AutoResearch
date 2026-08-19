"""第二条伪完成路径（#253）与 critic 产物不绑定单元（#252）。

真实时间线（cutover E2E，`matmul_bench_20260813T065235Z_e2e`）：Phase 2 critic 把
`- verdict: finish_ok` 写进 `critic_main.md`，引擎恒读 `critic.md`（Phase 1 的
needs_revision），于是错误追加 c2 修订链；coordinator 再用 `complete --status skipped`
把 c1/c2 十个单元逐个清空——#222 封掉的「done 绕裁决」换个 status 字面又开了。负控实测
`skipped → close done → verify-close` 三连 rc=0。

判据全部落在引擎行为上，与模型无关。
"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ENGINE = str(Path(__file__).resolve().parent.parent / "ar-workflow-engine.py")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, ENGINE, *args], capture_output=True, text=True)


def project(units: list[dict], **queue_extra) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ar_skipgate_"))
    queue = {"mode": "autoresearch_loop", "current_cycle": 1, "max_cycles": 3,
             "units": units, **queue_extra}
    (root / "workflow_queue.json").write_text(json.dumps(queue, ensure_ascii=False, indent=2))
    return root


def units_of(root: Path) -> dict[str, dict]:
    queue = json.loads((root / "workflow_queue.json").read_text())
    return {u["id"]: u for u in queue["units"]}


def write_critic(root: Path, *, verdict: str, mtime_before: str | None = None) -> None:
    path = root / "critic.md"
    units = units_of(root)
    critic = next(
        (unit for unit in units.values() if unit.get("type") == "critic"
         and unit.get("status") == "running"),
        next((unit for unit in units.values() if unit.get("type") == "critic"), {}),
    )
    path.write_text(
        f"- unit: {critic.get('id', 'critic_main')}\n"
        f"- cycle: {int(critic.get('cycle', 0) or 0)}\n"
        f"- verdict: {verdict}\n"
        "- required_next_focus: none\n",
        encoding="utf-8",
    )
    if mtime_before:
        # 把产物文件的 mtime 拨到单元开始之前，模拟「读到的是上一轮的 critic」。
        ts = datetime.fromisoformat(mtime_before.replace("Z", "+00:00")).timestamp() - 60
        os.utime(path, (ts, ts))
    unit_id = str(critic.get("id") or "critic_main")
    cycle = int(critic.get("cycle", 0) or 0)
    events_path = root / "workflow_events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ] if events_path.exists() else []
    issued_at = datetime.now(timezone.utc).isoformat()
    receipt_critics = []
    for role, model, identity in (
        ("critic", "model-a", "identity-a"),
        ("critic_secondary", "model-b", "identity-b"),
    ):
        receipt_critics.append({
            "role": role,
            "model": model,
            "model_identity": identity,
            "status": "ok",
            "verdict": verdict,
            "response_sha256": hashlib.sha256(
                f"{role}:{unit_id}:{cycle}:{verdict}".encode()
            ).hexdigest(),
        })
    event = {
        "schema_version": 1,
        "seq": len(events) + 1,
        "previous_hash": events[-1]["event_hash"] if events else "",
        "kind": "critic_receipt",
        "at": issued_at,
        "unit": unit_id,
        "cycle": cycle,
        "source": "ar-external-critic-mcp",
        "receipt": {
            "schema_version": 1,
            "request_id": str(uuid.uuid4()),
            "issued_at": issued_at,
            "producer_pid": 1,
            "unit": unit_id,
            "cycle": cycle,
            "artifact": "critic.md",
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "verdict": verdict,
            "critics": receipt_critics,
        },
    }
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    event["event_hash"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def write_analysis(root: Path) -> None:
    (root / "state.md").write_text(
        "- key_findings: the cycle answered its target\n"
        "- next_focus: none\n"
        "- stop_reason: the evidence is sufficient\n",
        encoding="utf-8",
    )


def prepare_skip_decision(root: Path, *, verdict: str = "finish_ok") -> None:
    write_analysis(root)
    analysis = run([
        "after-result-analysis", "--project-root", str(root),
        "--unit", "main_result_analysis", "--decision", "stop",
    ])
    assert analysis.returncode == 0, analysis.stdout
    claimed = run([
        "claim", "--project-root", str(root), "--worker", "critic",
        "--types", "critic",
    ])
    assert claimed.returncode == 0, claimed.stdout
    assert json.loads(claimed.stdout)["status"] == "claimed", claimed.stdout
    write_critic(root, verdict=verdict)


ANALYSIS_DONE = {
    "id": "main_result_analysis", "cycle": 1,
    "type": "result-analysis", "status": "done",
}
ANALYSIS_RUNNING = {
    "id": "main_result_analysis", "cycle": 1,
    "type": "result-analysis", "status": "running",
    "started_at": "2000-01-01T00:00:00+00:00",
}
CRITIC_RUNNING = {
    "id": "critic_main",
    "cycle": 1,
    "stage": "iteration",
    "type": "critic",
    "status": "running",
    "started_at": "2026-08-13T07:00:00+00:00",
    "blocked_by": "main_result_analysis",
}
CYCLE1 = [
    {"id": "planner_revise_c1", "type": "planning", "status": "pending", "cycle": 1},
    {"id": "runner_rerun_c1", "type": "run", "status": "pending", "cycle": 1},
]
CLOSE_PENDING = {"id": "close_if_done", "type": "close", "status": "pending"}


# ---- complete --status skipped 不再是万能橡皮擦 ----

def test_skipped_on_an_adjudicated_unit_is_rejected():
    """三型单元的完成定义就是裁决；skipped 同样是「不会再做」的终态声明。"""
    for unit_type in ("result-analysis", "critic", "blind-review"):
        root = project([{"id": "u1", "type": unit_type, "status": "running"}])

        proc = run(["complete", "--project-root", str(root), "--unit", "u1",
                    "--status", "skipped"])

        assert proc.returncode == 6, f"{unit_type}: {proc.stdout}"
        assert units_of(root)["u1"]["status"] == "running", unit_type


def test_skipped_on_a_revision_cycle_unit_is_rejected():
    """修订链只能整链按裁决跳（skip-cycle），不能逐个擦掉。"""
    root = project([dict(u) for u in CYCLE1])

    proc = run(["complete", "--project-root", str(root), "--unit", "runner_rerun_c1",
                "--status", "skipped"])

    assert proc.returncode == 6, proc.stdout
    assert "skip-cycle" in proc.stdout, "拒绝时要告诉 coordinator 正确的入口"
    assert units_of(root)["runner_rerun_c1"]["status"] == "pending"


def test_verify_close_rejects_an_unsanctioned_cycle_skip():
    """E2E 负控的三连 rc=0 就是这里放过的：skipped 不问出处即视为合法终态。"""
    skipped = [dict(u, status="skipped") for u in CYCLE1]
    root = project([dict(ANALYSIS_DONE), *skipped,
                    dict(CLOSE_PENDING, status="done")])
    write_critic(root, verdict="finish_ok")
    (root / "blind_review.md").write_text(
        "- n_reviews: 2\n- avg_rating: 7.0\n- decision: accept\n", encoding="utf-8")

    proc = run(["verify-close", "--project-root", str(root)])

    assert proc.returncode == 1, proc.stdout
    assert "skipped" in proc.stdout


# ---- skip-cycle：整链跳过的唯一入口，本身就是裁决 ----

def test_skip_cycle_ignores_free_text_stop_reason():
    """E2E-3 实况：state.md 写着 `stop_reason: none (continue to Phase 2)`。

    字面非空的「继续」被当成了停止依据，五个 c2 单元全被跳掉——语义反转。裁决只认
    引擎在 after-result-analysis 落账的 decision=stop；state.md 文本只作展示，写什么
    都不构成跳链依据。
    """
    root = project([dict(ANALYSIS_DONE), *[dict(u) for u in CYCLE1]])
    write_critic(root, verdict="finish_ok")
    (root / "state.md").write_text(
        "- stop_reason: none (continue to Phase 2)\n", encoding="utf-8")

    proc = run(["skip-cycle", "--project-root", str(root), "--cycle", "1"])

    assert proc.returncode != 0, proc.stdout
    assert units_of(root)["runner_rerun_c1"]["status"] == "pending"


def test_the_stop_record_comes_from_the_adjudication_command():
    """after-result-analysis --decision stop 是唯一的落账入口，落完 skip-cycle 才放行。"""
    root = project([
        dict(ANALYSIS_RUNNING),
        *[dict(u) for u in CYCLE1]])
    prepare_skip_decision(root)

    proc = run(["skip-cycle", "--project-root", str(root), "--cycle", "1"])
    assert proc.returncode == 0, proc.stdout
    assert units_of(root)["runner_rerun_c1"]["status"] == "skipped"


def test_skip_cycle_refuses_without_a_fresh_finish_ok():
    """critic 还在说 needs_revision（或产物是旧的）时，链不能被清。"""
    root = project([dict(ANALYSIS_RUNNING), *[dict(u) for u in CYCLE1]])
    prepare_skip_decision(root, verdict="needs_revision")

    proc = run(["skip-cycle", "--project-root", str(root), "--cycle", "1"])

    assert proc.returncode != 0, proc.stdout
    assert units_of(root)["runner_rerun_c1"]["status"] == "pending"


def test_skip_cycle_moots_the_cycle_and_verify_close_accepts_it():
    root = project([dict(ANALYSIS_RUNNING), *[dict(u) for u in CYCLE1], dict(CLOSE_PENDING)])
    prepare_skip_decision(root)

    proc = run(["skip-cycle", "--project-root", str(root), "--cycle", "1"])
    assert proc.returncode == 0, proc.stdout

    units = units_of(root)
    for uid in ("planner_revise_c1", "runner_rerun_c1"):
        assert units[uid]["status"] == "skipped"
        assert str(units[uid].get("reason", "")).startswith("cycle_mooted"), units[uid]
    queue = json.loads((root / "workflow_queue.json").read_text())
    assert queue["cycle_status"]["1"]["status"] == "skipped", "E2E 里 cycle_status 停在 pending 与单元矛盾"

    (root / "blind_review.md").write_text(
        "- n_reviews: 2\n- avg_rating: 7.0\n- decision: accept\n", encoding="utf-8")
    done = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])
    assert done.returncode == 0, done.stdout
    verdict = run(["verify-close", "--project-root", str(root)])
    assert verdict.returncode == 0, verdict.stdout


def test_a_mooted_cycle_may_contain_an_adjudicated_unit():
    """skip-cycle 的前置检查就是对整条链的裁决，链里的 result-analysis 一并被 moot。

    首个走新门的 E2E 实测过反面：coordinator 正确用 skip-cycle 清掉 c2（出处齐全、
    盲审 reject 如实入档、诚实 close），verify-close 却因链里的 result_analysis_c2
    是裁决型 skipped 而把整个 run 卡成永远 not_done，supervisor 烧完 6 次重启退 2。
    """
    cycle = [*[dict(u) for u in CYCLE1],
             {"id": "result_analysis_c1", "type": "result-analysis",
              "status": "pending", "cycle": 1}]
    root = project([dict(ANALYSIS_RUNNING), *cycle, dict(CLOSE_PENDING)])
    prepare_skip_decision(root)

    proc = run(["skip-cycle", "--project-root", str(root), "--cycle", "1"])
    assert proc.returncode == 0, proc.stdout
    assert units_of(root)["result_analysis_c1"]["status"] == "skipped"

    (root / "blind_review.md").write_text(
        "- n_reviews: 2\n- avg_rating: 7.0\n- decision: accept\n", encoding="utf-8")
    done = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])
    assert done.returncode == 0, done.stdout

    verdict = run(["verify-close", "--project-root", str(root)])
    assert verdict.returncode == 0, \
        f"被合法 moot 的裁决型单元不该卡住收尾：{verdict.stdout}"


# ---- critic 产物要属于本轮（#252，与盲审的 report_belongs_to 同构） ----

def test_after_critic_refuses_a_stale_critic_report():
    """E2E 实况：critic.md 是 Phase 1 的 needs_revision，Phase 2 的 finish_ok 在别的文件名下。

    引擎读到旧 verdict 就追加了一条谁都不需要的修订链。产物比单元开始时间旧 = 本轮没有
    critic 产物，单元退回 pending 等真产物，什么都不许追加。
    """
    root = project([dict(ANALYSIS_DONE), dict(CRITIC_RUNNING)])
    write_critic(root, verdict="needs_revision",
                 mtime_before=CRITIC_RUNNING["started_at"])

    proc = run(["after-critic", "--project-root", str(root), "--unit", "critic_main"])

    units = units_of(root)
    assert units["critic_main"]["status"] == "pending", proc.stdout
    assert not [u for u in units.values() if int(u.get("cycle", 0) or 0) >= 2], \
        "旧 verdict 不许拉起新修订链"


def test_after_critic_proceeds_with_a_fresh_report():
    root = project([dict(ANALYSIS_DONE), dict(CRITIC_RUNNING)])
    write_critic(root, verdict="finish_ok")
    time.sleep(0.05)

    proc = run(["after-critic", "--project-root", str(root), "--unit", "critic_main"])

    assert proc.returncode == 0, proc.stdout
    assert units_of(root)["critic_main"]["status"] == "done"
