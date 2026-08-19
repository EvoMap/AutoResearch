"""Cycle-bound adjudication and engine-owned skip provenance regressions."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest


ENGINE = str(Path(__file__).resolve().parent.parent / "ar-workflow-engine.py")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, ENGINE, *args], capture_output=True, text=True)


def project(units: list[dict], *, current_cycle: int = 2, **queue_extra) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ar_cycle_integrity_"))
    queue = {
        "mode": "autoresearch_loop",
        "current_cycle": current_cycle,
        "max_cycles": 4,
        "units": units,
        **queue_extra,
    }
    (root / "workflow_queue.json").write_text(
        json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def queue_of(root: Path) -> dict:
    return json.loads((root / "workflow_queue.json").read_text(encoding="utf-8"))


def units_of(root: Path) -> dict[str, dict]:
    return {unit["id"]: unit for unit in queue_of(root)["units"]}


def write_analysis(root: Path, *, stale_before: str | None = None, valid: bool = True) -> None:
    path = root / "state.md"
    text = (
        "- key_findings: the current cycle answered its target\n"
        "- next_focus: none\n"
        "- stop_reason: the current evidence is sufficient\n"
        if valid
        else "# State\n\nNo structured analysis fields were written.\n"
    )
    path.write_text(text, encoding="utf-8")
    if stale_before:
        timestamp = datetime.fromisoformat(stale_before.replace("Z", "+00:00")).timestamp() - 60
        os.utime(path, (timestamp, timestamp))


def write_critic(root: Path, verdict: str = "finish_ok") -> None:
    units = units_of(root)
    critic = next(
        (unit for unit in units.values() if unit.get("type") == "critic"),
        {"id": "critic", "cycle": 0},
    )
    unit_id = str(critic.get("id") or "critic")
    cycle = int(critic.get("cycle", 0) or 0)
    artifact = root / "critic.md"
    artifact.write_text(
        f"- unit: {unit_id}\n"
        f"- cycle: {cycle}\n"
        f"- verdict: {verdict}\n"
        "- required_next_focus: none\n"
        "- optional_next_focus: none\n"
        "- stop_reason: none\n",
        encoding="utf-8",
    )
    events_path = root / "workflow_events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ] if events_path.exists() else []
    critics = []
    for role, model, identity in (
        ("critic", "model-a", "identity-a"),
        ("critic_secondary", "model-b", "identity-b"),
    ):
        critics.append({
            "role": role,
            "model": model,
            "model_identity": identity,
            "status": "ok",
            "verdict": verdict,
            "response_sha256": hashlib.sha256(
                f"{role}:{unit_id}:{cycle}:{verdict}".encode()
            ).hexdigest(),
        })
    issued_at = datetime.now(timezone.utc).isoformat()
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
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "verdict": verdict,
            "critics": critics,
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


def write_blind_review(root: Path) -> None:
    (root / "blind_review.md").write_text(
        "- n_reviews: 1\n- avg_rating: 7.0\n- decision: accept\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("unit_type", "command"),
    [
        ("result-analysis", "after-result-analysis"),
        ("critic", "after-critic"),
        ("blind-review", "after-blind-review"),
    ],
)
@pytest.mark.parametrize("status", ["pending", "done"])
def test_adjudication_commands_require_a_running_unit(
    unit_type: str, command: str, status: str
) -> None:
    root = project([
        {"id": "u", "cycle": 0, "type": unit_type, "status": status},
    ], current_cycle=0)
    write_analysis(root)
    write_critic(root)
    write_blind_review(root)

    proc = run([command, "--project-root", str(root), "--unit", "u"])

    assert proc.returncode != 0, proc.stdout
    assert units_of(root)["u"]["status"] == status


@pytest.mark.parametrize(
    ("artifact", "response_status", "unit_status"),
    [
        ("missing", "pending", "pending"),
        ("stale", "pending", "pending"),
        ("unparsable", "retry_required", "running"),
    ],
)
def test_after_result_analysis_requires_a_fresh_structured_artifact(
    artifact: str,
    response_status: str,
    unit_status: str,
) -> None:
    started_at = "2026-08-14T00:00:00+00:00"
    root = project(
        [
            {
                "id": "result_analysis_c1",
                "cycle": 1,
                "type": "result-analysis",
                "status": "running",
                "started_at": started_at,
            }
        ],
        current_cycle=2,
    )
    if artifact == "stale":
        write_analysis(root, stale_before=started_at)
    elif artifact == "unparsable":
        write_analysis(root, valid=False)

    proc = run(
        [
            "after-result-analysis",
            "--project-root",
            str(root),
            "--unit",
            "result_analysis_c1",
            "--decision",
            "stop",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["status"] == response_status, proc.stdout
    assert units_of(root)["result_analysis_c1"]["status"] == unit_status
    assert not [unit for unit in units_of(root).values() if unit.get("type") == "critic"]


def test_after_result_analysis_requires_an_engine_start_timestamp() -> None:
    root = project(
        [
            {
                "id": "result_analysis_c1",
                "cycle": 1,
                "type": "result-analysis",
                "status": "running",
            }
        ],
        current_cycle=1,
    )
    write_analysis(root)

    proc = run(
        [
            "after-result-analysis",
            "--project-root",
            str(root),
            "--unit",
            "result_analysis_c1",
            "--decision",
            "stop",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["status"] == "pending", proc.stdout
    assert units_of(root)["result_analysis_c1"]["status"] == "pending"
    assert "last_analysis_decision" not in queue_of(root)


def test_after_result_analysis_records_the_unit_cycle_not_the_global_cycle() -> None:
    root = project(
        [
            {
                "id": "result_analysis_c1",
                "cycle": 1,
                "type": "result-analysis",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
        current_cycle=2,
    )
    write_analysis(root)

    proc = run(
        [
            "after-result-analysis",
            "--project-root",
            str(root),
            "--unit",
            "result_analysis_c1",
            "--decision",
            "stop",
        ]
    )

    assert proc.returncode == 0, proc.stdout
    assert queue_of(root)["last_analysis_decision"]["cycle"] == 1


def test_after_result_analysis_rejects_replaying_a_done_unit() -> None:
    root = project(
        [
            {
                "id": "result_analysis_c1",
                "cycle": 1,
                "type": "result-analysis",
                "status": "done",
            }
        ],
        current_cycle=2,
    )
    write_analysis(root)

    proc = run(
        [
            "after-result-analysis",
            "--project-root",
            str(root),
            "--unit",
            "result_analysis_c1",
            "--decision",
            "stop",
        ]
    )

    assert proc.returncode != 0, proc.stdout
    assert units_of(root)["result_analysis_c1"]["status"] == "done"


def test_skip_cycle_rejects_an_analysis_decision_from_another_cycle() -> None:
    root = project(
        [
            {
                "id": "result_analysis_c1",
                "cycle": 1,
                "type": "result-analysis",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            },
            {"id": "runner_c2", "cycle": 2, "type": "run", "status": "pending"},
        ],
        current_cycle=2,
    )
    write_analysis(root)
    analysis = run(
        [
            "after-result-analysis",
            "--project-root",
            str(root),
            "--unit",
            "result_analysis_c1",
            "--decision",
            "stop",
        ]
    )
    assert analysis.returncode == 0, analysis.stdout
    claimed = run(
        ["claim", "--project-root", str(root), "--worker", "critic", "--types", "critic"]
    )
    assert claimed.returncode == 0, claimed.stdout
    write_critic(root)

    proc = run(["skip-cycle", "--project-root", str(root), "--cycle", "2"])

    assert proc.returncode != 0, proc.stdout
    assert units_of(root)["runner_c2"]["status"] == "pending"


def test_skip_cycle_rejects_a_critic_from_another_cycle() -> None:
    root = project(
        [
            {
                "id": "result_analysis_c2",
                "cycle": 2,
                "type": "result-analysis",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            },
            {
                "id": "critic_c1",
                "cycle": 1,
                "type": "critic",
                "status": "running",
                "started_at": "2000-01-02T00:00:00+00:00",
            },
            {"id": "runner_c2", "cycle": 2, "type": "run", "status": "pending"},
        ],
        current_cycle=2,
    )
    write_analysis(root)
    analysis = run(
        [
            "after-result-analysis",
            "--project-root",
            str(root),
            "--unit",
            "result_analysis_c2",
            "--decision",
            "stop",
        ]
    )
    assert analysis.returncode == 0, analysis.stdout
    write_critic(root)

    proc = run(["skip-cycle", "--project-root", str(root), "--cycle", "2"])

    assert proc.returncode != 0, proc.stdout
    assert units_of(root)["runner_c2"]["status"] == "pending"


def prepare_valid_skip(root: Path, analysis_id: str) -> None:
    write_analysis(root)
    analysis = run(
        [
            "after-result-analysis",
            "--project-root",
            str(root),
            "--unit",
            analysis_id,
            "--decision",
            "stop",
        ]
    )
    assert analysis.returncode == 0, analysis.stdout
    claimed = run(
        ["claim", "--project-root", str(root), "--worker", "critic", "--types", "critic"]
    )
    assert claimed.returncode == 0, claimed.stdout
    assert json.loads(claimed.stdout)["status"] == "claimed", claimed.stdout
    write_critic(root)


def test_skip_cycle_does_not_sweep_close_or_blind_review() -> None:
    root = project(
        [
            {
                "id": "result_analysis_c2_source",
                "cycle": 2,
                "type": "result-analysis",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            },
            {"id": "runner_c2", "cycle": 2, "type": "run", "status": "pending"},
            {"id": "blind_c2", "cycle": 2, "type": "blind-review", "status": "pending"},
            {"id": "close_if_done", "cycle": 2, "type": "close", "status": "pending"},
        ],
        current_cycle=2,
    )
    prepare_valid_skip(root, "result_analysis_c2_source")

    proc = run(["skip-cycle", "--project-root", str(root), "--cycle", "2"])

    assert proc.returncode == 0, proc.stdout
    units = units_of(root)
    assert units["runner_c2"]["status"] == "skipped"
    assert units["blind_c2"]["status"] == "pending"
    assert units["close_if_done"]["status"] == "pending"


def test_verify_close_rejects_a_queue_only_cycle_mooted_prefix() -> None:
    skipped = {
        "id": "result_analysis_c1",
        "cycle": 1,
        "type": "result-analysis",
        "status": "skipped",
        "reason": "cycle_mooted:forged_by_queue_edit",
    }
    close = {"id": "close_if_done", "cycle": 1, "type": "close", "status": "done"}
    root = project([skipped, close], current_cycle=1, engine_seq=7)
    mirror = {
        **queue_of(root),
        "units": [
            {**skipped, "status": "pending", "reason": ""},
            {**close, "status": "pending"},
        ],
    }
    (root / "workflow_queue.engine.json").write_text(
        json.dumps(mirror, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    proc = run(["verify-close", "--project-root", str(root)])

    assert proc.returncode != 0, proc.stdout
    assert "skip" in proc.stdout.lower()


def forged_skip_queue() -> tuple[dict, dict]:
    reason = "cycle_mooted:analysis_stop=result_analysis_c1@cycle1;critic=finish_ok"
    analysis = {
        "cycle": 1,
        "unit": "result_analysis_c1",
        "decision": "stop",
        "artifact": "state.md",
        "artifact_mtime_ns": 1,
        "at": "2026-08-14T00:00:00+00:00",
    }
    critic = {
        "cycle": 1,
        "unit": "external_critic_after_result_analysis_c1",
        "verdict": "finish_ok",
        "artifact": "critic.md",
        "artifact_mtime_ns": 1,
    }
    record = {
        "schema_version": 1,
        "cycle": 1,
        "units": ["runner_c1"],
        "analysis": analysis,
        "critic": critic,
        "reason": reason,
        "at": "2026-08-14T00:00:01+00:00",
    }
    live = {
        "mode": "autoresearch_loop",
        "current_cycle": 1,
        "max_cycles": 3,
        "engine_seq": 7,
        "last_analysis_decision": analysis,
        "cycle_skip_records": {"1": record},
        "cycle_status": {
            "1": {
                "status": "skipped",
                "analysis_decision": analysis,
                "critic_decision": critic,
                "skip_record": record,
            }
        },
        "units": [
            {
                "id": "runner_c1",
                "cycle": 1,
                "type": "run",
                "status": "skipped",
                "reason": reason,
                "ended_at": "2026-08-14T00:00:01+00:00",
            },
            {"id": "close_if_done", "cycle": 1, "type": "close", "status": "pending"},
        ],
    }
    mirror = {
        "mode": "autoresearch_loop",
        "current_cycle": 1,
        "max_cycles": 3,
        "engine_seq": 7,
        "cycle_status": {"1": {"status": "running"}},
        "units": [
            {"id": "runner_c1", "cycle": 1, "type": "run", "status": "pending"},
            {"id": "close_if_done", "cycle": 1, "type": "close", "status": "pending"},
        ],
    }
    return live, mirror


def test_forged_skip_provenance_cannot_be_laundered_into_the_engine_mirror() -> None:
    root = Path(tempfile.mkdtemp(prefix="ar_skip_laundering_"))
    live, mirror = forged_skip_queue()
    (root / "workflow_queue.json").write_text(
        json.dumps(live, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "workflow_queue.engine.json").write_text(
        json.dumps(mirror, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_blind_review(root)

    ordinary_write = run(["ready", "--project-root", str(root)])
    close = run([
        "complete", "--project-root", str(root), "--unit", "close_if_done", "--status", "done",
    ])
    verified = run(["verify-close", "--project-root", str(root)])

    assert ordinary_write.returncode == 7, ordinary_write.stdout
    assert close.returncode != 0, close.stdout
    assert verified.returncode != 0, verified.stdout
    assert json.loads((root / "workflow_queue.engine.json").read_text(encoding="utf-8")) == mirror


def test_forged_adjudication_state_cannot_be_laundered_into_the_engine_mirror() -> None:
    root = project([
        {"id": "result_analysis_c1", "cycle": 1, "type": "result-analysis", "status": "pending"},
    ], current_cycle=1)
    initialized = run(["init", "--project-root", str(root)])
    assert initialized.returncode == 0, initialized.stdout
    mirror = json.loads(
        (root / "workflow_queue.engine.json").read_text(encoding="utf-8")
    )
    queue = queue_of(root)
    queue["units"][0].update({
        "status": "running",
        "started_at": "2000-01-01T00:00:00+00:00",
    })
    (root / "workflow_queue.json").write_text(
        json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ordinary_write = run(["ready", "--project-root", str(root)])

    assert ordinary_write.returncode == 7, ordinary_write.stdout
    rejected = json.loads(ordinary_write.stdout)
    assert "units.adjudication_state" in rejected["protected_fields"]
    assert json.loads(
        (root / "workflow_queue.engine.json").read_text(encoding="utf-8")
    ) == mirror


@pytest.mark.parametrize(("initial", "edited"), [("pending", "done"), ("done", "pending")])
def test_queue_regression_rejects_ordinary_status_edits(initial: str, edited: str) -> None:
    root = project([
        {"id": "code_c1", "cycle": 1, "type": "coding", "status": initial},
    ], current_cycle=1)
    initialized = run(["init", "--project-root", str(root)])
    assert initialized.returncode == 0, initialized.stdout
    mirror = json.loads(
        (root / "workflow_queue.engine.json").read_text(encoding="utf-8")
    )
    queue = queue_of(root)
    queue["units"][0]["status"] = edited
    (root / "workflow_queue.json").write_text(
        json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ordinary_write = run(["ready", "--project-root", str(root)])

    assert ordinary_write.returncode == 7, ordinary_write.stdout
    rejected = json.loads(ordinary_write.stdout)
    assert "units.engine_state" in rejected["protected_fields"]
    assert json.loads(
        (root / "workflow_queue.engine.json").read_text(encoding="utf-8")
    ) == mirror


def test_close_uses_an_accurate_reason_for_skip_provenance_gaps() -> None:
    skipped = {
        "id": "runner_c1",
        "cycle": 1,
        "type": "run",
        "status": "skipped",
        "reason": "cycle_mooted:forged_by_queue_edit",
    }
    root = project(
        [skipped, {"id": "close_if_done", "cycle": 1, "type": "close", "status": "pending"}],
        current_cycle=1,
    )
    write_blind_review(root)

    proc = run([
        "complete", "--project-root", str(root), "--unit", "close_if_done", "--status", "done",
    ])

    assert proc.returncode == 4, proc.stdout
    assert json.loads(proc.stdout)["reason"] == "completion_evidence_incomplete"


def test_engine_recorded_skip_allows_a_mooted_adjudication_unit() -> None:
    root = project(
        [
            {
                "id": "result_analysis_c2_source",
                "cycle": 2,
                "type": "result-analysis",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            },
            {
                "id": "result_analysis_c2_mooted",
                "cycle": 2,
                "type": "result-analysis",
                "status": "pending",
            },
            {"id": "close_if_done", "cycle": 2, "type": "close", "status": "pending"},
        ],
        current_cycle=2,
    )
    prepare_valid_skip(root, "result_analysis_c2_source")
    skipped = run(["skip-cycle", "--project-root", str(root), "--cycle", "2"])
    assert skipped.returncode == 0, skipped.stdout
    (root / "blind_review.md").write_text(
        "- n_reviews: 1\n- avg_rating: 7.0\n- decision: accept\n", encoding="utf-8"
    )
    closed = run(
        [
            "complete",
            "--project-root",
            str(root),
            "--unit",
            "close_if_done",
            "--status",
            "done",
        ]
    )
    assert closed.returncode == 0, closed.stdout

    verified = run(["verify-close", "--project-root", str(root)])

    assert verified.returncode == 0, verified.stdout
    assert units_of(root)["result_analysis_c2_mooted"]["status"] == "skipped"
