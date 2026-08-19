"""盲审门不能被竞态掏空。

真实时间线（`data/projects/matmul_smoke_official_20260811T224011/decisions.log`）：

    14:46:57Z  选中 blind_review_after_result_analysis_c1
    14:47:18Z  标 done，同一秒 after_blind_review outcome=close_appended rating=None
    14:48:00Z  autoresearch_done blind_review_rating=7.0   ← 此刻不存在任何盲审文件
    14:53:xxZ  blind_review.md 才落盘：avg_rating 3.0 / decision reject

按引擎自己的规则（<5.5 且有弱点且预算允许 → 追加修订轮），那次 close 不该发生。三个
成因里两个在引擎：读不到文件时直接判 `blind_review_unavailable` 并 close，以及不校验
盲审单元有没有产物就允许标 done。

判据落在引擎行为上，与模型无关——门的意义正是在模型不听话时兜住。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = str(Path(__file__).resolve().parent.parent / "ar-workflow-engine.py")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, ENGINE, *args], capture_output=True, text=True)


def project(units: list[dict], **queue_extra) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ar_blind_gate_"))
    queue = {"mode": "autoresearch_loop", "current_cycle": 0, "max_cycles": 3,
             "units": units, **queue_extra}
    (root / "workflow_queue.json").write_text(json.dumps(queue, ensure_ascii=False, indent=2))
    return root


BLIND_UNIT = {
    "id": "blind_review_c1",
    "type": "blind-review",
    "status": "running",
    "started_at": "2000-01-01T00:00:00+00:00",
}


def write_review(root: Path, *, rating: float, n_reviews: int = 2,
                 decision: str = "reject", weaknesses: str = "证据不足") -> None:
    (root / "blind_review.md").write_text(
        f"- n_reviews: {n_reviews}\n- avg_rating: {rating}\n- decision: {decision}\n"
        f"- calibration_gap: 3.0\n- top_weaknesses: {weaknesses}\n",
        encoding="utf-8")


def start_blind_review(root: Path) -> None:
    selected = run(["next-prompt", "--project-root", str(root)])
    assert selected.returncode == 0, selected.stdout
    queue = json.loads((root / "workflow_queue.json").read_text(encoding="utf-8"))
    unit = next(unit for unit in queue["units"] if unit["id"] == "blind_review_c1")
    assert unit["status"] == "running", selected.stdout


# ---- 产物没落盘时不能判 ----

def test_a_missing_report_puts_the_unit_back_instead_of_closing():
    """文件不在多半是 reviewer 还没跑完，不是「盲审做不了」。"""
    root = project([dict(BLIND_UNIT)])

    out = json.loads(run(["after-blind-review", "--project-root", str(root),
                          "--unit", "blind_review_c1"]).stdout)

    assert out["outcome"] == "blind_review_pending_artifact"
    queue = json.loads((root / "workflow_queue.json").read_text())
    unit = next(u for u in queue["units"] if u["id"] == "blind_review_c1")
    assert unit["status"] == "pending", "单元要退回去等，不能标 done"
    assert not [u for u in queue["units"] if u["type"] == "close"], "不能追加 close"


def test_an_absent_report_never_concedes_no_matter_how_often_asked():
    """等待预算只属于「文件在但零评审」。文件从未存在时，第二次、第 N 次调用都退回
    pending——sentinel 实测 32 秒连打两次就把预算烧掉换走了 unavailable close（#151）。"""
    root = project([dict(BLIND_UNIT)])

    run(["after-blind-review", "--project-root", str(root), "--unit", "blind_review_c1"])
    start_blind_review(root)
    second = json.loads(run(["after-blind-review", "--project-root", str(root),
                             "--unit", "blind_review_c1"]).stdout)

    assert second["outcome"] == "blind_review_pending_artifact"
    queue = json.loads((root / "workflow_queue.json").read_text())
    assert not [u for u in queue["units"] if u["type"] == "close"]


def test_a_report_that_arrives_during_the_wait_is_judged_on_its_merits():
    """这正是那次跑批的情形：文件晚到，而内容是 3.0/reject，该追加修订轮而不是 close。"""
    root = project([dict(BLIND_UNIT)])

    run(["after-blind-review", "--project-root", str(root), "--unit", "blind_review_c1"])
    start_blind_review(root)
    write_review(root, rating=3.0)
    out = json.loads(run(["after-blind-review", "--project-root", str(root),
                          "--unit", "blind_review_c1"]).stdout)

    assert "blind_revision_cycle" in out["outcome"], out["outcome"]
    queue = json.loads((root / "workflow_queue.json").read_text())
    assert not [u for u in queue["units"] if u["type"] == "close"]


def test_a_good_rating_still_closes():
    """门只拦缺证据的情况，达标的照常走。"""
    root = project([dict(BLIND_UNIT)])
    write_review(root, rating=8.0, decision="accept")

    out = json.loads(run(["after-blind-review", "--project-root", str(root),
                          "--unit", "blind_review_c1"]).stdout)

    assert out["outcome"].startswith("close_appended")


# ---- close 那一步再核一次 ----

CLOSE_UNIT = {"id": "close_if_done", "type": "close", "status": "pending",
              "blocked_by": "blind_review_c1"}


def test_close_is_refused_while_the_review_is_missing():
    """close 落地后 coordinator 就输出 AUTORESEARCH_DONE，所以这一步不可逆。

    判据是产物不是单元状态：把单元标 done 是模型做得到的事，写出一份带 n_reviews 的
    报告不是。
    """
    root = project([{**BLIND_UNIT, "status": "done"}, dict(CLOSE_UNIT)])

    done = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])

    assert done.returncode == 4, done.stdout
    assert json.loads(done.stdout)["reason"] == "completion_evidence_incomplete"


def test_close_is_refused_when_the_review_recorded_no_reviewer():
    """文件在但 n_reviews=0 是另一种失败，不能因为「文件在」就放行。"""
    root = project([{**BLIND_UNIT, "status": "done"}, dict(CLOSE_UNIT)])
    write_review(root, rating=0.0, n_reviews=0)

    done = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])

    assert done.returncode == 4
    assert "n_reviews" in json.loads(done.stdout)["missing"][0]


def test_close_goes_through_once_the_review_is_real():
    root = project([{**BLIND_UNIT, "status": "done"}, dict(CLOSE_UNIT)])
    write_review(root, rating=8.0, decision="accept")

    done = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])

    assert done.returncode == 0, done.stdout


def test_a_recorded_concession_is_not_relitigated():
    """真实降级（文件在、零评审、等过一轮）不该在 close 再拦一次而卡死。

    文件不在的 unavailable 不算：引擎在缺文件时根本不会追加这种 close，出现即伪造，
    close 那一步要重判（见 test_adjudication_hardening 的 forged 用例）。"""
    root = project([{**BLIND_UNIT, "status": "done"},
                    {**CLOSE_UNIT, "reason": "blind_review_unavailable"}])
    write_review(root, rating=0.0, n_reviews=0)

    done = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])

    assert done.returncode == 0, done.stdout


def test_a_run_without_blind_review_units_is_unaffected():
    """没有盲审单元的队列不该被这道门影响。"""
    root = project([{"id": "close_if_done", "type": "close", "status": "pending"}])

    done = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])

    assert done.returncode == 0, done.stdout


# ---- 文件在但没有评审，也要等一轮 ----

def test_a_report_with_no_reviewer_also_earns_one_wait():
    """SKILL 的契约字面是「盲审 blocked（n_reviews=0）时重试 ≤ 1 次」。

    上一版只等「文件不在」那种，于是 n_reviews=0 第一次就被放行成 unavailable close。
    两种都是产物没到位，reviewer 再跑一次就可能有。
    """
    root = project([dict(BLIND_UNIT)])
    write_review(root, rating=0.0, n_reviews=0, decision="blocked")

    out = json.loads(run(["after-blind-review", "--project-root", str(root),
                          "--unit", "blind_review_c1"]).stdout)

    assert out["outcome"] == "blind_review_pending_artifact"
    queue = json.loads((root / "workflow_queue.json").read_text())
    assert not [u for u in queue["units"] if u["type"] == "close"]


def test_a_reviewer_that_shows_up_on_the_retry_is_judged():
    """等这一轮的意义：第二次真的有评审了，就按评审结果走，而不是按 unavailable。"""
    root = project([dict(BLIND_UNIT)])
    write_review(root, rating=0.0, n_reviews=0, decision="blocked")
    run(["after-blind-review", "--project-root", str(root), "--unit", "blind_review_c1"])

    start_blind_review(root)
    write_review(root, rating=3.0, n_reviews=2)
    out = json.loads(run(["after-blind-review", "--project-root", str(root),
                          "--unit", "blind_review_c1"]).stdout)

    assert "blind_revision_cycle" in out["outcome"], out["outcome"]


def test_still_no_reviewer_after_the_wait_concedes():
    """等完了还是零评审，才按 unavailable 收场——预算是一次，不是无限。"""
    root = project([dict(BLIND_UNIT)])
    write_review(root, rating=0.0, n_reviews=0, decision="blocked")

    run(["after-blind-review", "--project-root", str(root), "--unit", "blind_review_c1"])
    start_blind_review(root)
    write_review(root, rating=0.0, n_reviews=0, decision="blocked")
    second = json.loads(run(["after-blind-review", "--project-root", str(root),
                             "--unit", "blind_review_c1"]).stdout)

    assert second["outcome"].startswith("close_appended")
    queue = json.loads((root / "workflow_queue.json").read_text())
    assert next(u for u in queue["units"] if u["type"] == "close")["reason"] == \
        "blind_review_unavailable"


# ---- 报告没照合同的字面写 ----

# 2026-08-13 cutover E2E 的真实产物（matmul_bench_20260813T065235Z_e2e）：haiku 把合同
# 里的 `- n_reviews: 1` 写成了 `- **Reviewer Count**: 1`，加粗加换词，于是一份 7.8/10
# 的 ACCEPT 在账本上等于「盲审做不成」。原文留作回归用例（#241）。
BOLD_REPORT = (Path(__file__).resolve().parent / "fixtures"
               / "blind_review_bold_field_names.md").read_text(encoding="utf-8")


def engine_module():
    spec = importlib.util.spec_from_file_location("ar_workflow_engine_under_test", ENGINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_real_report_that_bolded_and_renamed_the_fields_is_still_one_accept():
    root = project([dict(BLIND_UNIT)])
    (root / "blind_review.md").write_text(BOLD_REPORT, encoding="utf-8")

    report = engine_module().read_blind_review_report(root)

    assert report["n_reviews"] == 1
    assert report["avg_rating"] == 7.8
    assert report["decision"] == "accept"


def test_one_real_reviewer_still_does_not_satisfy_the_panel_contract():
    """历史单评审报告可以解析，但不能再作为双模型盲审通过。"""
    root = project([dict(BLIND_UNIT)])
    (root / "blind_review.md").write_text(BOLD_REPORT, encoding="utf-8")

    out = json.loads(run(["after-blind-review", "--project-root", str(root),
                          "--unit", "blind_review_c1"]).stdout)

    assert out["outcome"] == "blind_review_pending_artifact", out["outcome"]
    queue = json.loads((root / "workflow_queue.json").read_text())
    assert not [unit for unit in queue["units"] if unit["type"] == "close"]


def test_underscores_inside_a_value_survive_the_relaxed_matching():
    """放宽的只是字段名。值里的下划线是内容：`phase_2_complete` 被削一下，
    stop_reason 的下游判断会全部落空。"""
    parse = engine_module().parse_bullet_field

    assert parse("- stop_reason: phase_2_complete\n", "stop_reason") == ["phase_2_complete"]
    assert parse("- **avg_rating**: 7.8\n", "avg_rating") == ["7.8"]


def test_a_declared_zero_is_a_failed_review_not_a_format_problem():
    """reviewer 如实写下「一次评审都没成」，是真降级，不该被算成格式没对上。"""
    root = project([dict(BLIND_UNIT)])
    (root / "blind_review.md").write_text("- **n_reviews**: 0\n- **avg_rating**: 0.0\n",
                                          encoding="utf-8")

    report = engine_module().read_blind_review_report(root)

    assert report["n_reviews"] == 0
    assert report["n_reviews_declared"] is True


def test_a_report_that_declares_nothing_is_not_recorded_as_a_failed_review():
    """等完预算仍读不出任何合同字段，说明是格式没对上，不是盲审没做成。

    两者共用 `blind_review_unavailable` 的话，账本上再也分不出「其实评审过」，
    而那个 reason 是 close 那一步的免检牌——于是格式问题会被静默放行。
    """
    root = project([dict(BLIND_UNIT)])
    (root / "blind_review.md").write_text("这次评审写成了散文，一个合同字段都没有。\n",
                                          encoding="utf-8")

    run(["after-blind-review", "--project-root", str(root), "--unit", "blind_review_c1"])
    start_blind_review(root)
    (root / "blind_review.md").write_text("这次评审写成了散文，一个合同字段都没有。\n",
                                          encoding="utf-8")
    second = json.loads(run(["after-blind-review", "--project-root", str(root),
                             "--unit", "blind_review_c1"]).stdout)

    assert second["outcome"].startswith("close_appended")
    queue = json.loads((root / "workflow_queue.json").read_text())
    assert next(u for u in queue["units"] if u["type"] == "close")["reason"] == \
        "blind_review_unparsable"

    done = run(["complete", "--project-root", str(root), "--unit", "close_if_done",
                "--status", "done"])

    assert done.returncode == 4, done.stdout
