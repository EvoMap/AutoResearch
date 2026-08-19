"""裁决命令与完成权威的硬化（#151 的 sentinel 复现）。

真实时间线（`data/projects/sentinel_official_20260812T163654/decisions.log`，
分支 refactor/official-cc-runtime）：

    08:39:30  after_result_analysis unit=run_pilot_experiment   ← type=run 的单元
    08:40:03  after_blind_review present=False waits=1/1
    08:40:35  after_blind_review → unavailable close            ← 32 秒后第二次调用
    08:40:45  close_if_done 标 done                             ← 六个单元还 pending

三个缝加一个洞：裁决命令不验单元类型；文件从未存在也能烧掉 wait 换 unavailable close；
close 落 done 不看队列里还有什么；而 supervisor 直接解析 workflow_queue.json，队列本身
可以被 coordinator 手改。判据全部落在引擎行为上，与模型无关。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = str(Path(__file__).resolve().parent.parent / "ar-workflow-engine.py")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, ENGINE, *args], capture_output=True, text=True)


def project(units: list[dict], **queue_extra) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ar_hardening_"))
    queue = {"mode": "autoresearch_loop", "current_cycle": 0, "max_cycles": 3,
             "units": units, **queue_extra}
    (root / "workflow_queue.json").write_text(json.dumps(queue, ensure_ascii=False, indent=2))
    return root


def units_of(root: Path) -> dict[str, dict]:
    queue = json.loads((root / "workflow_queue.json").read_text())
    return {u["id"]: u for u in queue["units"]}


def write_review(root: Path, *, rating: float, n_reviews: int = 2) -> None:
    (root / "blind_review.md").write_text(
        f"- n_reviews: {n_reviews}\n- avg_rating: {rating}\n- decision: reject\n"
        f"- calibration_gap: 3.0\n- top_weaknesses: 证据不足\n", encoding="utf-8")


def write_analysis(root: Path) -> None:
    (root / "state.md").write_text(
        "- key_findings: the analysis completed\n"
        "- next_focus: continue\n"
        "- stop_reason: none\n",
        encoding="utf-8",
    )


def restart_blind_review(root: Path) -> None:
    selected = run(["next-prompt", "--project-root", str(root)])
    assert selected.returncode == 0, selected.stdout
    assert units_of(root)["blind_review_c1"]["status"] == "running"


RUN_UNIT = {"id": "run_pilot_experiment", "type": "run", "status": "running"}
BLIND_UNIT = {
    "id": "blind_review_c1",
    "type": "blind-review",
    "status": "running",
    "started_at": "2000-01-01T00:00:00+00:00",
}

# 完成即裁决的三型单元，和它们各自的裁决命令。
ADJUDICATED = [
    ("result-analysis", "after-result-analysis"),
    ("critic", "after-critic"),
    ("blind-review", "after-blind-review"),
]


# ---- 裁决命令要验单元类型 ----

def test_after_result_analysis_rejects_a_run_unit():
    """sentinel 就是拿 type=run 的单元换到了 critic 链。"""
    root = project([dict(RUN_UNIT)])

    proc = run(["after-result-analysis", "--project-root", str(root),
                "--unit", "run_pilot_experiment"])

    assert proc.returncode != 0, proc.stdout
    units = units_of(root)
    assert units["run_pilot_experiment"]["status"] == "running", "单元不许被顺手标 done"
    assert not [u for u in units.values() if u.get("type") == "critic"], "critic 链不许被拉起"


def test_after_critic_rejects_a_non_critic_unit():
    root = project([dict(RUN_UNIT)])
    proc = run(["after-critic", "--project-root", str(root), "--unit", "run_pilot_experiment"])
    assert proc.returncode != 0


def test_after_blind_review_rejects_a_non_blind_unit():
    root = project([dict(RUN_UNIT)])
    proc = run(["after-blind-review", "--project-root", str(root),
                "--unit", "run_pilot_experiment"])
    assert proc.returncode != 0


def test_matching_types_still_work():
    """门只拦错型的调用，对的照常走。"""
    root = project([{"id": "pilot_result_analysis", "type": "result-analysis",
                     "status": "running", "started_at": "2000-01-01T00:00:00+00:00"}])
    write_analysis(root)
    proc = run(["after-result-analysis", "--project-root", str(root),
                "--unit", "pilot_result_analysis"])
    assert proc.returncode == 0, proc.stdout
    assert units_of(root)["pilot_result_analysis"]["status"] == "done"


# ---- complete 不许替裁决命令签字 ----

def test_complete_cannot_stand_in_for_the_adjudication_command():
    """官方 run 2（`r2_official_run2_20260812T192500`）：coordinator 在同一秒把 c1 尾部
    三个单元 `complete --status done` 批掉，其中 result_analysis_c1 没走
    after-result-analysis，critic/盲审链因此从未被追加，队列排空却永远差一个 close。

    #152 给裁决命令加了类型校验，堵的是「拿错型单元换裁决」；这里堵反方向——绕开裁决
    命令直接把单元标完成（#218）。
    """
    for unit_type, command in ADJUDICATED:
        root = project([{"id": "u", "type": unit_type, "status": "running"}])
        write_review(root, rating=8.0)  # 盲审那型连产物都齐，照样不许

        proc = run(["complete", "--project-root", str(root), "--unit", "u", "--status", "done"])

        assert proc.returncode == 6, (unit_type, proc.stdout)
        payload = json.loads(proc.stdout)
        assert payload["reason"] == "adjudication_required", payload
        assert command in payload["command"], payload
        assert units_of(root)["u"]["status"] == "running", f"{unit_type} 不许被标 done"


def test_a_coordinator_cannot_mark_required_work_failed_to_unblock_the_queue():
    """真实 E2E 中 coordinator 在 review receipt 被拒后用 failed 放行了 main run/analysis。

    失败的 attempt 由 supervisor 记录；当前 required unit 必须保持 running，下一次会话才能
    恢复同一单元。需要人工介入时用 blocked 停住，不能把 failed 当作「做完了」。
    """
    root = project([
        {"id": "review_main_experiment", "type": "review", "status": "running"},
        {"id": "run_main_experiment", "type": "run", "status": "pending",
         "blocked_by": "review_main_experiment"},
    ])

    proc = run(["complete", "--project-root", str(root),
                "--unit", "review_main_experiment", "--status", "failed"])

    assert proc.returncode == 6, proc.stdout
    assert json.loads(proc.stdout)["reason"] == "required_unit_failure_is_retryable"
    assert units_of(root)["review_main_experiment"]["status"] == "running"
    selected = run(["next-prompt", "--project-root", str(root)])
    assert "next_unit = review_main_experiment" in selected.stdout
    assert units_of(root)["run_main_experiment"]["status"] == "pending"


def test_a_queue_only_failed_predecessor_does_not_unlock_downstream_work():
    """coordinator 能改 queue；调度器也不能把伪造的 failed 当作依赖已满足。"""
    root = project([
        {"id": "review_main_experiment", "type": "review", "status": "failed"},
        {"id": "run_main_experiment", "type": "run", "status": "pending",
         "blocked_by": "review_main_experiment"},
    ])

    selected = run(["next-prompt", "--project-root", str(root)])

    assert "next_unit = run_main_experiment" not in selected.stdout
    assert "blocked/failed" in selected.stdout
    assert units_of(root)["run_main_experiment"]["status"] == "pending"


def test_an_adjudication_command_from_a_worker_that_lost_its_lease_is_refused():
    """这三型单元现在只能由裁决命令判完，裁决命令因此成了抢占模式下唯一的回写入口。
    它得和被它取代的 complete 一样认租约，否则「防双写」这条只是换个命令就绕开了。"""
    root = project([{"id": "u", "type": "result-analysis", "status": "running",
                     "claimed_by": "w2"}])

    proc = run(["after-result-analysis", "--project-root", str(root), "--unit", "u",
                "--worker", "w1"])

    assert proc.returncode == 3, proc.stdout
    assert json.loads(proc.stdout)["reason"] == "claim_lost"
    assert units_of(root)["u"]["status"] == "running"


def test_adjudicating_a_claimed_unit_hands_the_lease_back():
    root = project([{"id": "u", "type": "result-analysis", "status": "running",
                     "claimed_by": "w1", "started_at": "2000-01-01T00:00:00+00:00",
                     "lease_expires_at": "2099-01-01T00:00:00+00:00"}])
    write_analysis(root)

    proc = run(["after-result-analysis", "--project-root", str(root), "--unit", "u",
                "--worker", "w1"])

    assert proc.returncode == 0, proc.stdout
    unit = units_of(root)["u"]
    assert unit["status"] == "done"
    assert "claimed_by" not in unit and "lease_expires_at" not in unit, unit


def test_the_worker_prompt_for_an_adjudicated_unit_points_at_the_adjudication_command():
    """门和照着提示干活的人要读同一处。提示里还写着 `complete --status done`，
    worker 就会照做，然后撞上 exit 6。"""
    root = project([{"id": "analysis", "type": "result-analysis", "status": "pending"}])

    prompt = run(["claim", "--project-root", str(root), "--worker", "w1", "--prompt"]).stdout

    assert "after-result-analysis" in prompt, prompt
    assert "state.md" in prompt, prompt
    assert "本次领取后重新写入" in prompt, prompt
    assert "--status done" not in prompt, prompt


def test_next_prompt_explains_that_the_adjudication_artifact_must_follow_selection():
    """真实 E2E 连续领取五次后立刻裁决，旧 state.md 每次都被 freshness 门拒绝。"""
    root = project([{"id": "analysis", "type": "result-analysis", "status": "pending"}])
    write_analysis(root)

    prompt = run(["next-prompt", "--project-root", str(root)]).stdout

    assert "state.md" in prompt, prompt
    assert "本次领取后重新写入" in prompt, prompt
    assert "after-result-analysis" in prompt, prompt


def test_a_stale_artifact_rejection_returns_the_recovery_order():
    root = project([{"id": "analysis", "type": "result-analysis", "status": "running",
                     "started_at": "2099-01-01T00:00:00+00:00"}])
    write_analysis(root)

    proc = run(["after-result-analysis", "--project-root", str(root), "--unit", "analysis"])

    assert proc.returncode == 0, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["status"] == "pending", payload
    assert payload["stale"] is True, payload
    assert payload["required_artifact"] == "state.md", payload
    assert "next-prompt/claim" in payload["recovery"], payload
    assert "领取后重新写入 state.md" in payload["recovery"], payload


def test_an_unparsable_critic_keeps_the_current_claim_for_same_attempt_recovery():
    root = project([
        {
            "id": "critic_after_pilot",
            "cycle": 0,
            "stage": "pilot",
            "type": "critic",
            "status": "pending",
        }
    ])
    claimed = run([
        "claim",
        "--project-root",
        str(root),
        "--worker",
        "critic-worker",
        "--lease-seconds",
        "300",
    ])
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    first = units_of(root)["critic_after_pilot"]
    started_at = first["started_at"]
    (root / "critic.md").write_text(
        "This artifact has no structured critic verdict.\n",
        encoding="utf-8",
    )

    rejected = run([
        "after-critic",
        "--project-root",
        str(root),
        "--unit",
        "critic_after_pilot",
        "--worker",
        "critic-worker",
    ])

    assert rejected.returncode == 0, rejected.stdout + rejected.stderr
    payload = json.loads(rejected.stdout)
    assert payload["status"] == "retry_required", payload
    unit = units_of(root)["critic_after_pilot"]
    assert unit["status"] == "running"
    assert unit["claimed_by"] == "critic-worker"
    assert unit["started_at"] == started_at
    assert unit["claim_attempts"] == 1

    duplicate = run([
        "claim",
        "--project-root",
        str(root),
        "--worker",
        "critic-worker",
    ])
    assert duplicate.returncode == 0, duplicate.stdout + duplicate.stderr
    assert json.loads(duplicate.stdout)["status"] == "empty"
    assert units_of(root)["critic_after_pilot"]["claim_attempts"] == 1


# ---- 文件从未存在，等多少轮都不换 close ----

def test_an_absent_report_never_concedes_to_close():
    """sentinel 32 秒连打两次烧掉 wait 拿到 unavailable close——现在烧不掉。

    文件从未存在说明 reviewer 从没跑完过，这不是「盲审做不了」而是「盲审没发生」；
    收不了场就交给 supervisor 以非零码收人，不能让引擎替它编一个降级。
    """
    root = project([dict(BLIND_UNIT)])

    first = json.loads(run(["after-blind-review", "--project-root", str(root),
                            "--unit", "blind_review_c1"]).stdout)
    restart_blind_review(root)
    second = json.loads(run(["after-blind-review", "--project-root", str(root),
                             "--unit", "blind_review_c1"]).stdout)

    assert first["outcome"] == "blind_review_pending_artifact"
    assert second["outcome"] == "blind_review_pending_artifact", second
    units = units_of(root)
    assert units["blind_review_c1"]["status"] == "pending"
    assert not [u for u in units.values() if u.get("type") == "close"], "close 永不追加"


def test_a_zero_review_report_still_earns_exactly_one_wait_then_concedes():
    """「文件在但零评审」是真实尝试过的失败，保留一轮等待后按 unavailable 收场。"""
    root = project([dict(BLIND_UNIT)])
    write_review(root, rating=0.0, n_reviews=0)

    run(["after-blind-review", "--project-root", str(root), "--unit", "blind_review_c1"])
    restart_blind_review(root)
    write_review(root, rating=0.0, n_reviews=0)
    second = json.loads(run(["after-blind-review", "--project-root", str(root),
                             "--unit", "blind_review_c1"]).stdout)

    assert second["outcome"].startswith("close_appended")


# ---- close 落 done 前要看队列 ----

PENDING_CHAIN = [
    {"id": "spawn_agents", "type": "agent", "status": "pending"},
    {"id": "plan_gate", "type": "planning", "status": "pending"},
]


def test_close_is_refused_while_other_units_are_active():
    """sentinel 的 close 落 done 时六个单元还 pending。"""
    root = project([*PENDING_CHAIN,
                    {**BLIND_UNIT, "status": "done"},
                    {"id": "close_if_done", "type": "close", "status": "pending",
                     "reason": "blind_review_rating_8.0"}])
    write_review(root, rating=8.0)

    proc = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])

    assert proc.returncode == 4, proc.stdout
    assert json.loads(proc.stdout)["reason"] == "queue_still_active"


def test_close_goes_through_when_the_queue_is_really_drained():
    root = project([{"id": "spawn_agents", "type": "agent", "status": "done"},
                    {**BLIND_UNIT, "status": "done"},
                    {"id": "close_if_done", "type": "close", "status": "pending",
                     "reason": "blind_review_rating_8.0"}])
    write_review(root, rating=8.0)

    proc = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])

    assert proc.returncode == 0, proc.stdout


def test_a_forged_concession_without_a_report_is_relitigated():
    """coordinator 直接把 reason=unavailable 写进队列 JSON 也过不了 close：
    降级的两个前提（真实尝试过=文件在、等过一轮）少一个都不算数。"""
    root = project([{**BLIND_UNIT, "status": "done"},
                    {"id": "close_if_done", "type": "close", "status": "pending",
                     "reason": "blind_review_unavailable"}])

    proc = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])

    assert proc.returncode == 4, proc.stdout


# ---- 完成权威：supervisor 问引擎，不自己读 JSON ----

def test_verify_close_rejects_a_hand_edited_queue():
    """终极绕法是绕开引擎直接改 workflow_queue.json。verify-close 把完成判定
    收回引擎：close done 但队列还有 active 单元 → 非零。"""
    root = project([*PENDING_CHAIN,
                    {**BLIND_UNIT, "status": "done"},
                    {"id": "close_if_done", "type": "close", "status": "done",
                     "reason": "blind_review_rating_8.0"}])
    write_review(root, rating=8.0)

    proc = run(["verify-close", "--project-root", str(root)])

    assert proc.returncode != 0, proc.stdout


def test_verify_close_rejects_a_close_without_artifact():
    root = project([{**BLIND_UNIT, "status": "done"},
                    {"id": "close_if_done", "type": "close", "status": "done",
                     "reason": "blind_review_unavailable"}])
    proc = run(["verify-close", "--project-root", str(root)])
    assert proc.returncode != 0


def test_verify_close_accepts_a_genuine_finish():
    root = project([{"id": "spawn_agents", "type": "agent", "status": "done"},
                    {**BLIND_UNIT, "status": "done"},
                    {"id": "close_if_done", "type": "close", "status": "done",
                     "reason": "blind_review_rating_8.0"}])
    write_review(root, rating=8.0)

    proc = run(["verify-close", "--project-root", str(root)])

    assert proc.returncode == 0, proc.stdout


def test_verify_close_reports_not_done_when_close_is_absent():
    root = project([dict(RUN_UNIT)])
    proc = run(["verify-close", "--project-root", str(root)])
    assert proc.returncode != 0
