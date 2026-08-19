"""盲审门守住了 close，工作流却不收敛。

#113/#114 之后两次实跑（fork 与 official 各一次）终态相同：队列排空、没有 close_if_done、
真实盲审晚到且是 reject。门本身对了——不该 close 的没 close——但没人把流程接回去，于是
运行停在一个既不结束也无法继续的状态。

三个成因都在同一个状态机边界上：

1. 盲审单元可以被手工标 done，绕过 after-blind-review 的等待
2. 队列排空且没有 close 时，next-prompt 只输出一段让模型自己判断的提示
3. init 在已有队列上会静默改写 max_cycles

判据是引擎行为，与模型无关：门的意义正是在模型不听话时兜住。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile

import pytest
from pathlib import Path

ENGINE = str(Path(__file__).resolve().parent.parent / "ar-workflow-engine.py")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, ENGINE, *args], capture_output=True, text=True)


def project(units: list[dict], **extra) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ar_converge_"))
    queue = {"mode": "autoresearch_loop", "current_cycle": 0, "max_cycles": 3,
             "units": units, **extra}
    (root / "workflow_queue.json").write_text(json.dumps(queue, ensure_ascii=False, indent=2))
    return root


def queue_of(root: Path) -> dict:
    return json.loads((root / "workflow_queue.json").read_text(encoding="utf-8"))


def write_review(root: Path, *, rating: float, n_reviews: int = 2, decision: str = "reject"):
    (root / "blind_review.md").write_text(
        f"- n_reviews: {n_reviews}\n- avg_rating: {rating}\n- decision: {decision}\n"
        f"- calibration_gap: 2.0\n- top_weaknesses: 证据不足\n", encoding="utf-8")


def start_blind_review(root: Path, unit_id: str = "blind_review_c1") -> None:
    selected = run(["next-prompt", "--project-root", str(root)])
    assert selected.returncode == 0, selected.stdout
    unit = next(unit for unit in queue_of(root)["units"] if unit["id"] == unit_id)
    assert unit["status"] == "running", selected.stdout


BLIND = {"id": "blind_review_c1", "type": "blind-review", "status": "pending"}


# ---- 1. 手工标 done 绕过等待 ----

def test_a_blind_review_unit_cannot_be_marked_done_without_a_report():
    """两次实跑的同一形状：after-blind-review 把单元退回 pending 等产物，
    数秒后 coordinator 直接 `complete --status done`，没有召唤 reviewer。

    把单元标 done 是模型做得到的事，写出一份带 n_reviews 的报告不是。
    """
    root = project([dict(BLIND)])

    done = run(["complete", "--project-root", str(root),
                "--unit", "blind_review_c1", "--status", "done"])

    assert done.returncode != 0, done.stdout
    assert queue_of(root)["units"][0]["status"] != "done"


def test_even_with_a_real_report_the_verdict_belongs_to_after_blind_review():
    """产物齐了也不能用 complete 收工：盲审单元的「完成」是那次裁决——按分数追加修订
    还是 close。用 complete 标 done 只写状态，队列会排空却永远差一个 close（#218）。"""
    root = project([dict(BLIND)])
    write_review(root, rating=3.0)

    done = run(["complete", "--project-root", str(root),
                "--unit", "blind_review_c1", "--status", "done"])

    assert done.returncode == 6, done.stdout
    assert queue_of(root)["units"][0]["status"] != "done"

    start_blind_review(root)
    write_review(root, rating=3.0)
    verdict = json.loads(run(["after-blind-review", "--project-root", str(root),
                              "--unit", "blind_review_c1"]).stdout)
    assert verdict["status"] == "ok", verdict


def test_other_unit_types_are_unaffected():
    """这道门只管盲审单元，别的照常。"""
    root = project([{"id": "code_gate", "type": "coding", "status": "running"}])

    done = run(["complete", "--project-root", str(root),
                "--unit", "code_gate", "--status", "done"])

    assert done.returncode == 0, done.stdout


# ---- 2. 排空且无 close：要能自己回到可执行状态 ----

def test_an_empty_queue_without_close_recovers_the_blind_review():
    """这是两次实跑的终态。原来只打印一段让模型自己判断的提示，于是运行卡死。

    判据是队列真的变了，不是提示里说了什么——提示要靠模型照做，恢复不该依赖它。
    """
    root = project([{**BLIND, "status": "done"},
                    {"id": "analysis", "type": "result-analysis", "status": "done"}])

    run(["next-prompt", "--project-root", str(root)])

    unit = next(u for u in queue_of(root)["units"] if u["id"] == "blind_review_c1")
    assert unit["status"] == "pending", "错标 done 的盲审单元要被恢复"


def test_the_recovered_unit_is_what_next_prompt_hands_out():
    """恢复之后要能立刻被领走，否则下一轮还是空。"""
    root = project([{**BLIND, "status": "done"},
                    {"id": "analysis", "type": "result-analysis", "status": "done"}])

    run(["next-prompt", "--project-root", str(root)])
    again = run(["next-prompt", "--project-root", str(root)])

    assert "blind_review_c1" in again.stdout, again.stdout


def test_a_legitimate_unavailable_close_is_not_requeued():
    """负控：等过预算、显式降级过的那种，不能被恢复逻辑无限重新排队。"""
    root = project([{**BLIND, "status": "done"},
                    {"id": "close_if_done", "type": "close", "status": "done",
                     "reason": "blind_review_unavailable"}],
                   blind_review_artifact_waits=1)

    run(["next-prompt", "--project-root", str(root)])

    unit = next(u for u in queue_of(root)["units"] if u["id"] == "blind_review_c1")
    assert unit["status"] == "done", "合法降级过的不该被拉回来"


def test_a_finished_run_with_a_real_close_stays_finished():
    """有 close、有产物的正常收尾不受影响。"""
    root = project([{**BLIND, "status": "done"},
                    {"id": "close_if_done", "type": "close", "status": "done",
                     "reason": "blind_review_rating_8.0"}])
    write_review(root, rating=8.0, decision="accept")

    run(["next-prompt", "--project-root", str(root)])

    unit = next(u for u in queue_of(root)["units"] if u["id"] == "blind_review_c1")
    assert unit["status"] == "done"


# ---- 3. init 在已有队列上不能改配置 ----

def test_init_on_an_existing_queue_refuses_to_change_max_cycles():
    """官方侧那次跑批里三条 queue_initialized，最后一条把 max_cycles 从 3 改成 2。

    进度保留了，配置被后来的会话静默改写——resume 一次就换一套参数。
    """
    root = project([dict(BLIND)])

    done = run(["init", "--project-root", str(root), "--max-cycles", "2"])

    assert done.returncode != 0, done.stdout
    assert "3" in (done.stdout + done.stderr) and "2" in (done.stdout + done.stderr), \
        "要说清原值和新值"
    assert queue_of(root)["max_cycles"] == 3


def test_init_with_the_same_parameters_is_idempotent():
    """resume 会重跑 init，相同参数必须放行，否则每次续跑都红。"""
    root = project([dict(BLIND)])

    done = run(["init", "--project-root", str(root), "--max-cycles", "3"])

    assert done.returncode == 0, done.stdout


def test_init_without_the_flag_leaves_the_existing_value():
    root = project([dict(BLIND)])

    done = run(["init", "--project-root", str(root)])

    assert done.returncode == 0
    assert queue_of(root)["max_cycles"] == 3


def test_init_allows_phase_zero_bootstrap_files():
    """idea/state/decision bootstrap happens before the first engine init."""
    root = Path(tempfile.mkdtemp(prefix="ar_bootstrap_"))
    for name, content in (
        ("idea.md", "# Idea\n"),
        ("idea_provenance.json", '{"sha256":"bootstrap"}\n'),
        ("plan.md", ""),
        ("state.md", "# Initial state\n"),
        ("decisions.log", "phase 0 bootstrap\n"),
    ):
        (root / name).write_text(content, encoding="utf-8")

    initialized = run(["init", "--project-root", str(root), "--max-cycles", "3"])

    assert initialized.returncode == 0, initialized.stdout
    assert (root / "workflow_queue.engine.json").exists()
    assert (root / "workflow_events.jsonl").exists()


def test_init_refuses_to_rebuild_after_authority_files_disappear():
    """删除队列、镜像和事件账不能把已有项目伪装成 fresh project。"""
    root = project([dict(BLIND)])
    initialized = run(["init", "--project-root", str(root), "--max-cycles", "3"])
    assert initialized.returncode == 0, initialized.stdout

    (root / "state.md").write_text("# Existing run\n", encoding="utf-8")
    (root / "decisions.log").write_text(
        "2026-08-16T00:00:00Z | event=unit_marked unit=blind_review_c1 status=done\n",
        encoding="utf-8",
    )
    (root / "results").mkdir()
    (root / "results" / "run.log").write_text("[pilot] observation\n", encoding="utf-8")
    for name in ("workflow_queue.json", "workflow_queue.engine.json", "workflow_events.jsonl"):
        (root / name).unlink()

    rebuilt = run(["init", "--project-root", str(root), "--max-cycles", "3"])

    assert rebuilt.returncode == 5, rebuilt.stdout
    payload = json.loads(rebuilt.stdout)
    assert payload["reason"] == "incomplete_workflow_state"
    assert payload["history"] == ["results/run.log"]
    assert not (root / "workflow_queue.json").exists()


# ---- 状态机走通：晚到的盲审仍能推进 ----

def test_a_late_review_still_reaches_a_revision_cycle():
    """把两次实跑的完整时间线走一遍，验证它现在会收敛而不是卡住。

    等产物 → 单元被错标 done → 队列排空 → 恢复 → reviewer 这次真跑了 → 按 3.0/reject
    追加修订轮。这是那两次运行本该走到的地方。
    """
    root = project([dict(BLIND), {"id": "analysis", "type": "result-analysis", "status": "done"}])

    # 1. 产物还没到，退回等一轮
    start_blind_review(root)
    first = json.loads(run(["after-blind-review", "--project-root", str(root),
                            "--unit", "blind_review_c1"]).stdout)
    assert first["outcome"] == "blind_review_pending_artifact"

    # 2. coordinator 想直接标 done —— 现在被拒
    assert run(["complete", "--project-root", str(root),
                "--unit", "blind_review_c1", "--status", "done"]).returncode != 0

    # 3. 绕过 engine 手改队列会被 mirror 对账拒绝，不能洗成新的引擎状态
    queue = queue_of(root)
    next(u for u in queue["units"] if u["id"] == "blind_review_c1")["status"] = "done"
    (root / "workflow_queue.json").write_text(json.dumps(queue, ensure_ascii=False))
    rejected = run(["next-prompt", "--project-root", str(root)])
    assert rejected.returncode == 7, rejected.stdout
    (root / "workflow_queue.json").write_text(
        (root / "workflow_queue.engine.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    assert queue_of(root)["units"][0]["status"] == "pending"

    # 4. reviewer 这次真跑了，晚到的报告按内容判
    start_blind_review(root)
    write_review(root, rating=3.0)
    final = json.loads(run(["after-blind-review", "--project-root", str(root),
                            "--unit", "blind_review_c1"]).stdout)

    assert "blind_revision_cycle" in final["outcome"], final["outcome"]


def test_a_late_passing_review_closes_instead():
    """同一条路，分数达标时应当 close 而不是无限修订。"""
    root = project([dict(BLIND)])

    start_blind_review(root)
    run(["after-blind-review", "--project-root", str(root), "--unit", "blind_review_c1"])
    start_blind_review(root)
    write_review(root, rating=8.0, decision="accept")
    final = json.loads(run(["after-blind-review", "--project-root", str(root),
                            "--unit", "blind_review_c1"]).stdout)

    assert final["outcome"].startswith("close_appended")


def test_an_absent_artifact_ends_as_honestly_not_done():
    """负控：产物始终不来时必须停——但终点从「引擎替它降级 close」移到了
    「supervisor 用非零码交人」（#151）。

    旧版在等完预算后按 unavailable close，sentinel 实测 coordinator 32 秒连打两次
    就能烧掉预算拿到一个看似合法的收尾。现在缺文件永不 close：引擎侧的判据是
    verify-close 始终非零，永动机在 supervisor 的 MAX_RESTARTS 处终止。"""
    root = project([dict(BLIND)])

    start_blind_review(root)
    run(["after-blind-review", "--project-root", str(root), "--unit", "blind_review_c1"])
    start_blind_review(root)
    second = json.loads(run(["after-blind-review", "--project-root", str(root),
                             "--unit", "blind_review_c1"]).stdout)
    assert second["outcome"] == "blind_review_pending_artifact"

    assert not [u for u in queue_of(root)["units"] if u["type"] == "close"]
    verify = run(["verify-close", "--project-root", str(root)])
    assert verify.returncode != 0, "缺产物的 run 不许被判为 done"


# ---- 报告要属于它那一轮 ----

import time  # noqa: E402


def test_a_second_round_cannot_pass_on_the_first_round_report():
    """产物是全局单文件，而修订之后会再排一个盲审单元。

    只检查「文件在且 n_reviews >= 2」的话，第二轮可以拿第一轮的报告过门——#127 封住的
    「手工标 done」换个凭据又开了：从「没有报告」变成「上一轮的报告」。

    这道门原来长在 complete 上；#218 之后盲审单元只能由 after-blind-review 判完，门跟着
    搬到裁决那一侧。上一轮的报告和「报告还没来」是同一回事：单元退回 pending 等产物。
    """
    root = project([{"id": "blind_c1", "type": "blind-review", "status": "done"},
                    {"id": "blind_c2", "type": "blind-review", "status": "pending"}],
                   current_cycle=1)
    write_review(root, rating=3.0)
    time.sleep(1.1)
    run(["next-prompt", "--project-root", str(root)])       # 领取 c2，写 started_at

    verdict = json.loads(run(["after-blind-review", "--project-root", str(root),
                              "--unit", "blind_c2"]).stdout)

    assert verdict["outcome"] == "blind_review_pending_artifact", verdict
    units = {u["id"]: u for u in queue_of(root)["units"]}
    assert units["blind_c2"]["status"] == "pending"
    assert not [u for u in units.values() if u["type"] == "close"], "不许拿上一轮的报告 close"


def test_a_report_written_for_this_round_passes():
    root = project([{"id": "blind_c1", "type": "blind-review", "status": "done"},
                    {"id": "blind_c2", "type": "blind-review", "status": "pending"}],
                   current_cycle=1)
    write_review(root, rating=3.0)
    time.sleep(1.1)
    run(["next-prompt", "--project-root", str(root)])
    write_review(root, rating=8.0, decision="accept")       # reviewer 这一轮真跑了

    verdict = json.loads(run(["after-blind-review", "--project-root", str(root),
                              "--unit", "blind_c2"]).stdout)

    assert verdict["outcome"].startswith("close_appended"), verdict


def test_a_unit_without_a_start_time_waits_for_a_fresh_attempt():
    """缺少引擎起跑时间时无法证明报告属于本轮，必须退回再领取。"""
    root = project([{**BLIND, "status": "running"}])
    write_review(root, rating=8.0, decision="accept")

    verdict = json.loads(run(["after-blind-review", "--project-root", str(root),
                              "--unit", "blind_review_c1"]).stdout)

    assert verdict["status"] == "pending", verdict
    assert queue_of(root)["units"][0]["status"] == "pending"


def test_claiming_a_unit_again_resets_its_start_time():
    """单元被退回重来时保留旧时间戳，会让上一轮的产物看起来是这一轮写的。"""
    root = project([dict(BLIND)])
    run(["claim", "--project-root", str(root), "--types", "blind-review",
         "--worker", "w1"])
    first = next(u for u in queue_of(root)["units"] if u["id"] == "blind_review_c1")["started_at"]

    released = run(["release", "--project-root", str(root), "--unit", "blind_review_c1",
                    "--worker", "w1"])
    assert released.returncode == 0, released.stdout
    time.sleep(1.1)
    run(["claim", "--project-root", str(root), "--types", "blind-review",
         "--worker", "w1"])
    second = next(u for u in queue_of(root)["units"] if u["id"] == "blind_review_c1")["started_at"]

    assert second != first


# ---- 恢复只碰 done ----

@pytest.mark.parametrize("status", ["failed", "skipped"])
def test_a_real_failure_is_not_recovered(status):
    """`failed` / `skipped` 是明确下过的结论。改回 pending 等于吞掉 reviewer 的真实失败，
    而 Ralph 循环会一直重跑它。"""
    root = project([{**BLIND, "status": status}])

    run(["next-prompt", "--project-root", str(root)])

    assert queue_of(root)["units"][0]["status"] == status


def test_a_wrongly_done_unit_is_still_recovered():
    """正控：没有产物却被标 done 的，仍然要被拉回来。"""
    root = project([{**BLIND, "status": "done"}])

    run(["next-prompt", "--project-root", str(root)])

    assert queue_of(root)["units"][0]["status"] == "pending"
