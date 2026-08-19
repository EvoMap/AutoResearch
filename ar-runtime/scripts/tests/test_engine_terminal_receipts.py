"""Terminal authority, cycle binding, and run/review receipt regressions."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest


ENGINE = Path(__file__).resolve().parent.parent / "ar-workflow-engine.py"


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ENGINE), *args, "--project-root", str(root)],
        capture_output=True,
        text=True,
    )


def write_queue(root: Path, units: list[dict], *, current_cycle: int = 0) -> None:
    queue = {
        "mode": "autoresearch_loop",
        "schema_version": 1,
        "completion_authority_version": 1,
        "engine_seq": 7,
        "current_cycle": current_cycle,
        "max_cycles": 3,
        "units": units,
    }
    payload = json.dumps(queue, ensure_ascii=False, indent=2)
    (root / "workflow_queue.json").write_text(payload, encoding="utf-8")
    (root / "workflow_queue.engine.json").write_text(payload, encoding="utf-8")


def queue_of(root: Path) -> dict:
    return json.loads((root / "workflow_queue.json").read_text(encoding="utf-8"))


def units_of(root: Path) -> dict[str, dict]:
    return {unit["id"]: unit for unit in queue_of(root)["units"]}


def test_queue_only_review_completion_cannot_unlock_a_run(tmp_path: Path) -> None:
    write_queue(
        tmp_path,
        [
            {
                "id": "code_review",
                "cycle": 0,
                "type": "review",
                "status": "blocked",
                "started_at": "2000-01-01T00:00:00+00:00",
                "ended_at": "2000-01-01T00:01:00+00:00",
            },
            {
                "id": "run_pilot_experiment",
                "cycle": 0,
                "stage": "pilot",
                "type": "run",
                "status": "pending",
                "blocked_by": "code_review",
            },
        ],
    )
    mirror = json.loads(
        (tmp_path / "workflow_queue.engine.json").read_text(encoding="utf-8")
    )
    queue = queue_of(tmp_path)
    review = next(unit for unit in queue["units"] if unit["id"] == "code_review")
    review.update(
        {
            "status": "done",
            "ended_at": "2000-01-01T00:02:00+00:00",
            "result": "coordinator supplied completion",
        }
    )
    (tmp_path / "workflow_queue.json").write_text(
        json.dumps(queue, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    resumed = run(tmp_path, "next-prompt")

    assert resumed.returncode == 7, resumed.stdout
    rejected = json.loads(resumed.stdout)
    assert "units.engine_state" in rejected["protected_fields"]
    assert json.loads(
        (tmp_path / "workflow_queue.engine.json").read_text(encoding="utf-8")
    ) == mirror


def write_state(root: Path, unit: str, cycle: int) -> None:
    (root / "state.md").write_text(
        f"- analysis_unit: {unit}\n"
        f"- analysis_cycle: {cycle}\n"
        "- key_findings: current evidence\n"
        "- next_focus: diagnose the remaining gap\n"
        "- stop_reason: none\n",
        encoding="utf-8",
    )


def engine_event_hash(event: dict) -> str:
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def write_critic(
    root: Path,
    unit: str,
    cycle: int,
    verdict: str = "needs_revision",
    *,
    receipt: bool = True,
) -> None:
    artifact = root / "critic.md"
    artifact.write_text(
        f"- unit: {unit}\n"
        f"- cycle: {cycle}\n"
        f"- verdict: {verdict}\n"
        "- required_next_focus: diagnose the remaining gap\n"
        "- optional_next_focus: none\n"
        "- stop_reason: none\n",
        encoding="utf-8",
    )
    if not receipt:
        return
    events_path = root / "workflow_events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ] if events_path.exists() else []
    responses = []
    for role, model, identity in (
        ("critic", "model-a", "identity-a"),
        ("critic_secondary", "model-b", "identity-b"),
    ):
        responses.append({
            "role": role,
            "model": model,
            "model_identity": identity,
            "status": "ok",
            "verdict": verdict,
            "response_sha256": hashlib.sha256(
                f"{role}:{unit}:{cycle}:{verdict}".encode()
            ).hexdigest(),
        })
    event = {
        "schema_version": 1,
        "seq": len(events) + 1,
        "previous_hash": events[-1]["event_hash"] if events else "",
        "kind": "critic_receipt",
        "at": "2026-08-15T00:00:00+00:00",
        "unit": unit,
        "cycle": cycle,
        "source": "ar-external-critic-mcp",
        "receipt": {
            "schema_version": 1,
            "request_id": str(uuid.uuid4()),
            "issued_at": "2026-08-15T00:00:00+00:00",
            "producer_pid": 1,
            "unit": unit,
            "cycle": cycle,
            "artifact": "critic.md",
            "artifact_sha256": sha256(artifact),
            "verdict": verdict,
            "critics": responses,
        },
    }
    event["event_hash"] = engine_event_hash(event)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_review_receipt(root: Path, unit: str, cycle: int) -> None:
    artifact = root / "review.md"
    text = artifact.read_text(encoding="utf-8")
    fields = {}
    for line in text.split("---", 2)[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    events_path = root / "workflow_events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ] if events_path.exists() else []
    receipt = {
        "schema_version": 1,
        "request_id": str(uuid.uuid4()),
        "issued_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "producer_pid": 1,
        "unit": unit,
        "cycle": cycle,
        "artifact": "review.md",
        "artifact_sha256": sha256(artifact),
        "reviewer": fields["reviewer"],
        "model": fields["model"],
        "model_identity": fields["model_identity"],
        "blockers_count": int(fields["blockers_count"]),
    }
    event = {
        "schema_version": 1,
        "seq": len(events) + 1,
        "previous_hash": events[-1]["event_hash"] if events else "",
        "kind": "review_report_receipt",
        "at": receipt["issued_at"],
        "unit": unit,
        "cycle": cycle,
        "source": "ar-gemini-review-mcp",
        "receipt": receipt,
    }
    event["event_hash"] = engine_event_hash(event)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def rewrite_review_receipt(root: Path, mutation: str) -> None:
    events_path = root / "workflow_events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    receipt = events[-1]["receipt"]
    if mutation == "artifact_hash":
        receipt["artifact_sha256"] = "0" * 64
    elif mutation == "wrong_cycle":
        receipt["cycle"] = int(receipt["cycle"]) + 1
    elif mutation == "model_identity":
        receipt["model_identity"] = "forged-identity"
    else:
        raise AssertionError(f"unknown review receipt mutation: {mutation}")
    events[-1]["event_hash"] = engine_event_hash(events[-1])
    events_path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in events
        ),
        encoding="utf-8",
    )


def inferred_run_stage(unit: str) -> str:
    if "pilot" in unit:
        return "pilot"
    if "main" in unit:
        return "main"
    return "iteration"


def write_run_execution_event(
    root: Path,
    unit: str,
    cycle: int,
    attempt: Path,
    *,
    stage: str | None = None,
    argv: list[str] | None = None,
    environment: dict | None = None,
) -> str:
    venv = root / ".venv"
    python = venv / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    entrypoint = root / "code" / "main.py"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text("raise SystemExit(0)\n", encoding="utf-8")
    run_log = root / "results" / "run.log"
    run_log.parent.mkdir(parents=True, exist_ok=True)
    run_log.touch()
    selected_stage = stage or inferred_run_stage(unit)
    artifact_dir = root / "results" / "run_artifacts" / unit
    command = argv or [
        ".venv/bin/python",
        "code/main.py",
        "--stage",
        selected_stage,
        "--artifact-dir",
        str(artifact_dir.relative_to(root)),
        "--run-log",
        str(run_log.relative_to(root)),
    ]
    execution_environment = environment or {
        "kind": "venv",
        "prefix": ".venv",
        "python": ".venv/bin/python",
        "python_sha256": sha256(python),
    }
    events_path = root / "workflow_events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ] if events_path.exists() else []
    event = {
        "schema_version": 1,
        "seq": len(events) + 1,
        "previous_hash": events[-1]["event_hash"] if events else "",
        "kind": "run_execution",
        "at": "2026-08-14T18:01:00+00:00",
        "unit": unit,
        "cycle": cycle,
        "stage": selected_stage,
        "source": "execute-run",
        "started_at": "2026-08-14T18:00:00+00:00",
        "finished_at": "2026-08-14T18:01:00+00:00",
        "exit_code": 0,
        "argv": command,
        "cwd": ".",
        "environment": execution_environment,
        "artifact_dir": str(artifact_dir.relative_to(root)),
        "run_log": {
            "path": str(run_log.relative_to(root)),
            "before_bytes": 0,
            "before_sha256": hashlib.sha256(b"").hexdigest(),
            "after_bytes": 0,
            "after_sha256": hashlib.sha256(b"").hexdigest(),
        },
        "attempt": {
            "path": str(attempt.relative_to(root)),
            "sha256": sha256(attempt),
        },
        "contract_gaps": [],
    }
    event["event_hash"] = engine_event_hash(event)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event["event_hash"]


def write_run_receipt(
    root: Path,
    unit: str,
    cycle: int,
    *,
    execution_stage: str | None = None,
    execution_argv: list[str] | None = None,
    execution_environment: dict | None = None,
) -> Path:
    artifact_dir = root / "results" / "run_artifacts" / unit
    artifact_dir.mkdir(parents=True)
    raw = artifact_dir / "attempt-1.log"
    summary = artifact_dir / "summary.md"
    raw.write_text("exit_code=0\n", encoding="utf-8")
    summary.write_text("# Result\n\nmetric: 1\n", encoding="utf-8")
    execution_event_hash = write_run_execution_event(
        root,
        unit,
        cycle,
        raw,
        stage=execution_stage,
        argv=execution_argv,
        environment=execution_environment,
    )
    receipt = root / "results" / "run_receipts" / f"{unit}.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "unit": unit,
                "cycle": cycle,
                "status": "completed",
                "exit_code": 0,
                "started_at": "2026-08-14T18:00:00+00:00",
                "finished_at": "2026-08-14T18:01:00+00:00",
                "execution_event_hash": execution_event_hash,
                "artifacts": [
                    {
                        "path": str(raw.relative_to(root)),
                        "sha256": sha256(raw),
                    },
                    {
                        "path": str(summary.relative_to(root)),
                        "sha256": sha256(summary),
                    },
                ],
                "summary": {
                    "path": str(summary.relative_to(root)),
                    "sha256": sha256(summary),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return raw


def create_run_program(root: Path) -> tuple[Path, Path]:
    venv = root / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    entrypoint = root / "code" / "main.py"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text(
        "import argparse\n"
        "from pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--stage', required=True)\n"
        "p.add_argument('--artifact-dir', required=True)\n"
        "p.add_argument('--run-log', required=True)\n"
        "a = p.parse_args()\n"
        "art = Path(a.artifact_dir)\n"
        "art.mkdir(parents=True, exist_ok=True)\n"
        "(art / f'{a.stage}-observation.json').write_text('{\"ok\":true}\\n')\n"
        "with Path(a.run_log).open('a') as h: h.write(f'[{a.stage}] observation\\n')\n",
        encoding="utf-8",
    )
    return venv / "bin" / "python", entrypoint


def execute_run(
    root: Path,
    unit: str,
    argv: list[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ENGINE),
            "execute-run",
            "--project-root",
            str(root),
            "--unit",
            unit,
            "--",
            *argv,
        ],
        capture_output=True,
        text=True,
    )


def test_execute_run_records_the_actual_stage_scoped_command(tmp_path: Path) -> None:
    unit_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    python, entrypoint = create_run_program(tmp_path)
    artifact_dir = tmp_path / "results" / "run_artifacts" / unit_id
    run_log = tmp_path / "results" / "run.log"

    proc = execute_run(
        tmp_path,
        unit_id,
        [
            str(python),
            str(entrypoint),
            "--stage",
            "pilot",
            "--artifact-dir",
            str(artifact_dir),
            "--run-log",
            str(run_log),
        ],
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "completed"
    assert len(result["execution_event_hash"]) == 64
    assert (artifact_dir / "pilot-observation.json").is_file()
    events, problems = read_test_events(tmp_path)
    assert not problems
    assert events[-1]["kind"] == "run_execution"
    assert events[-1]["argv"][2:4] == ["--stage", "pilot"]


def test_execute_run_prepares_parallel_namespaces_under_a_readonly_root(
    tmp_path: Path,
) -> None:
    pilot = "run_pilot_experiment"
    main = "run_main_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": pilot,
                "cycle": 0,
                "stage": "pilot",
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            },
            {
                "id": main,
                "cycle": 0,
                "stage": "main",
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            },
        ],
    )
    python, entrypoint = create_run_program(tmp_path)

    def argv(unit: str, stage: str) -> list[str]:
        return [
            str(python),
            str(entrypoint),
            "--stage",
            stage,
            "--artifact-dir",
            str(tmp_path / "results" / "run_artifacts" / unit),
            "--run-log",
            str(tmp_path / "results" / "run.log"),
        ]

    first = execute_run(tmp_path, pilot, argv(pilot, "pilot"))

    assert first.returncode == 0, first.stdout + first.stderr
    artifact_root = tmp_path / "results" / "run_artifacts"
    assert artifact_root.stat().st_mode & 0o222 == 0
    assert (artifact_root / main).is_dir()
    assert (artifact_root / main).stat().st_mode & 0o200

    second = execute_run(tmp_path, main, argv(main, "main"))

    assert second.returncode == 0, second.stdout + second.stderr


def test_claim_prepares_each_parallel_run_namespace_before_execution(
    tmp_path: Path,
) -> None:
    unit_ids = ["run_branch_a", "run_branch_b"]
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "stage": "iteration",
                "type": "run",
                "status": "pending",
            }
            for unit_id in unit_ids
        ],
    )

    first = run(tmp_path, "claim", "--worker", "worker-a")
    second = run(tmp_path, "claim", "--worker", "worker-b")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    artifact_root = tmp_path / "results" / "run_artifacts"
    assert artifact_root.stat().st_mode & 0o222 == 0
    for unit_id in unit_ids:
        artifact_dir = artifact_root / unit_id
        assert artifact_dir.is_dir()
        assert artifact_dir.stat().st_mode & 0o200


def read_test_events(root: Path) -> tuple[list[dict], list[str]]:
    events = [
        json.loads(line)
        for line in (root / "workflow_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    problems = []
    previous = ""
    for index, event in enumerate(events, 1):
        if event.get("seq") != index or event.get("previous_hash") != previous:
            problems.append(f"broken event {index}")
        if event.get("event_hash") != engine_event_hash(event):
            problems.append(f"bad hash {index}")
        previous = event.get("event_hash", "")
    return events, problems


def test_execute_run_rejects_an_unscoped_command_before_it_starts(tmp_path: Path) -> None:
    unit_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    python, entrypoint = create_run_program(tmp_path)
    marker = tmp_path / "command-started"
    entrypoint.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")

    proc = execute_run(tmp_path, unit_id, [str(python), str(entrypoint)])

    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert not marker.exists()
    assert "argv 未绑定 --stage pilot" in proc.stdout


@pytest.mark.parametrize(
    ("option", "second_value"),
    [
        ("--stage", "main"),
        ("--artifact-dir", "results/run_artifacts/run_main_experiment"),
        ("--run-log", "results/other.log"),
    ],
)
def test_execute_run_rejects_duplicate_scope_options_before_it_starts(
    tmp_path: Path,
    option: str,
    second_value: str,
) -> None:
    unit_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    python, entrypoint = create_run_program(tmp_path)
    marker = tmp_path / "command-started"
    entrypoint.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    artifact_dir = tmp_path / "results" / "run_artifacts" / unit_id
    argv = [
        str(python),
        str(entrypoint),
        "--stage",
        "pilot",
        "--artifact-dir",
        str(artifact_dir),
        "--run-log",
        str(tmp_path / "results" / "run.log"),
        option,
        second_value,
    ]

    proc = execute_run(tmp_path, unit_id, argv)

    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert not marker.exists()
    assert f"{option} 必须恰好出现一次" in proc.stdout


def write_receipt_for_execution(
    root: Path,
    unit: str,
    cycle: int,
    execution: dict,
) -> None:
    artifact_dir = root / "results" / "run_artifacts" / unit
    summary = artifact_dir / "summary.md"
    summary.write_text("# Result\n\nstatus: completed\n", encoding="utf-8")
    artifacts = [
        {"path": str(path.relative_to(root)), "sha256": sha256(path)}
        for path in sorted(artifact_dir.rglob("*"))
        if path.is_file()
    ]
    event, _ = read_test_events(root)
    executed = next(
        item for item in reversed(event)
        if item.get("event_hash") == execution["execution_event_hash"]
    )
    receipt = root / "results" / "run_receipts" / f"{unit}.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "unit": unit,
                "cycle": cycle,
                "status": "completed",
                "exit_code": 0,
                "started_at": executed["started_at"],
                "finished_at": executed["finished_at"],
                "execution_event_hash": execution["execution_event_hash"],
                "artifacts": artifacts,
                "summary": {
                    "path": str(summary.relative_to(root)),
                    "sha256": sha256(summary),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_execute_run_event_and_terminal_receipt_complete_the_same_unit(
    tmp_path: Path,
) -> None:
    unit_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    python, entrypoint = create_run_program(tmp_path)
    artifact_dir = tmp_path / "results" / "run_artifacts" / unit_id
    executed = execute_run(
        tmp_path,
        unit_id,
        [
            str(python),
            str(entrypoint),
            "--stage",
            "pilot",
            "--artifact-dir",
            str(artifact_dir),
            "--run-log",
            str(tmp_path / "results" / "run.log"),
        ],
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
    write_receipt_for_execution(tmp_path, unit_id, 0, json.loads(executed.stdout))

    completed = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert units_of(tmp_path)[unit_id]["status"] == "done"


def test_run_receipt_must_reference_the_latest_execution_for_its_unit(
    tmp_path: Path,
) -> None:
    unit_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    python, entrypoint = create_run_program(tmp_path)
    artifact_dir = tmp_path / "results" / "run_artifacts" / unit_id
    argv = [
        str(python),
        str(entrypoint),
        "--stage",
        "pilot",
        "--artifact-dir",
        str(artifact_dir),
        "--run-log",
        str(tmp_path / "results" / "run.log"),
    ]
    succeeded = execute_run(tmp_path, unit_id, argv)
    assert succeeded.returncode == 0, succeeded.stdout + succeeded.stderr
    entrypoint.write_text("raise SystemExit(1)\n", encoding="utf-8")
    failed = execute_run(tmp_path, unit_id, argv)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    write_receipt_for_execution(tmp_path, unit_id, 0, json.loads(succeeded.stdout))

    completed = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert completed.returncode == 4, completed.stdout + completed.stderr
    assert "最新一次 run execution" in completed.stdout
    assert units_of(tmp_path)[unit_id]["status"] == "running"


def test_execute_run_prevents_pilot_from_creating_a_main_artifact_directory(
    tmp_path: Path,
) -> None:
    unit_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            },
            {
                "id": "run_main_experiment",
                "cycle": 0,
                "stage": "main",
                "type": "run",
                "status": "pending",
                "blocked_by": unit_id,
            },
        ],
    )
    python, entrypoint = create_run_program(tmp_path)
    entrypoint.write_text(
        "import argparse\n"
        "from pathlib import Path\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--stage'); p.add_argument('--artifact-dir'); p.add_argument('--run-log')\n"
        "a=p.parse_args()\n"
        "(Path(a.artifact_dir).parent / 'main').mkdir()\n"
        "(Path(a.artifact_dir).parent / 'main' / 'observation.json').write_text('{}')\n",
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "results" / "run_artifacts" / unit_id

    proc = execute_run(
        tmp_path,
        unit_id,
        [
            str(python),
            str(entrypoint),
            "--stage",
            "pilot",
            "--artifact-dir",
            str(artifact_dir),
            "--run-log",
            str(tmp_path / "results" / "run.log"),
        ],
    )

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert not (tmp_path / "results" / "run_artifacts" / "main").exists()
    events, _ = read_test_events(tmp_path)
    assert events[-1]["kind"] == "run_execution"
    assert events[-1]["exit_code"] != 0


def rewrite_last_receipt(root: Path, mutation: str) -> None:
    events_path = root / "workflow_events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    event = events[-1]
    receipt = event["receipt"]
    if mutation == "duplicate_identity":
        receipt["critics"][1]["model_identity"] = receipt["critics"][0][
            "model_identity"
        ]
    elif mutation == "missing_secondary":
        receipt["critics"] = receipt["critics"][:1]
    elif mutation == "artifact_hash":
        receipt["artifact_sha256"] = "0" * 64
    elif mutation == "wrong_cycle":
        receipt["cycle"] = 9
    elif mutation == "response_hash":
        receipt["critics"][0]["response_sha256"] = "not-a-sha256"
    elif mutation == "consensus":
        receipt["critics"][0]["verdict"] = "needs_revision"
    elif mutation == "secondary_skipped":
        receipt["critics"][1].update({
            "status": "skipped",
            "model": "",
            "model_identity": "",
            "verdict": None,
            "response_sha256": None,
        })
    else:
        raise AssertionError(f"unknown receipt mutation: {mutation}")
    event["event_hash"] = engine_event_hash(event)
    events_path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in events
        ),
        encoding="utf-8",
    )


def test_verify_close_rejects_queue_and_mirror_forged_together_without_engine_events(
    tmp_path: Path,
) -> None:
    units = [
        {
            "id": "result_analysis_c1",
            "cycle": 1,
            "type": "result-analysis",
            "status": "done",
            "ended_at": "2026-08-14T18:10:00Z",
        },
        {
            "id": "external_critic_after_result_analysis_c1",
            "cycle": 1,
            "type": "critic",
            "status": "done",
            "ended_at": "2026-08-14T18:11:00Z",
        },
        {
            "id": "blind_review_after_result_analysis_c1",
            "cycle": 1,
            "type": "blind-review",
            "status": "done",
            "ended_at": "2026-08-14T18:12:00Z",
        },
        {
            "id": "close_if_done",
            "cycle": 1,
            "type": "close",
            "status": "done",
            "ended_at": "2026-08-14T18:13:00Z",
        },
    ]
    write_queue(tmp_path, units, current_cycle=1)
    (tmp_path / "blind_review.md").write_text(
        "- unit: blind_review_after_result_analysis_c1\n"
        "- cycle: 1\n"
        "- n_reviews: 1\n"
        "- avg_rating: 3.0\n"
        "- decision: reject\n",
        encoding="utf-8",
    )

    proc = run(tmp_path, "verify-close")

    assert proc.returncode != 0, proc.stdout
    assert "engine event" in proc.stdout.lower()


def test_pilot_critic_does_not_append_a_revision_cycle_before_main_experiment(
    tmp_path: Path,
) -> None:
    critic_id = "external_critic_after_pilot_result_analysis"
    write_queue(
        tmp_path,
        [
            {
                "id": critic_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "critic",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
                "blocked_by": "pilot_result_analysis",
            },
            {
                "id": "planner_scale_up",
                "cycle": 0,
                "stage": "main",
                "type": "planning",
                "status": "pending",
                "blocked_by": critic_id,
            },
        ],
    )
    write_state(tmp_path, "pilot_result_analysis", 0)
    write_critic(tmp_path, critic_id, 0)

    proc = run(tmp_path, "after-critic", "--unit", critic_id)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["outcome"] == "pilot_critic_complete"
    queue = queue_of(tmp_path)
    assert queue["current_cycle"] == 0
    assert not [unit for unit in queue["units"] if unit.get("cycle") == 1]
    assert units_of(tmp_path)["planner_scale_up"]["status"] == "pending"


def test_forged_critic_without_a_producer_receipt_cannot_advance(
    tmp_path: Path,
) -> None:
    critic_id = "external_critic_after_pilot_result_analysis"
    write_queue(
        tmp_path,
        [
            {
                "id": critic_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "critic",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
                "blocked_by": "pilot_result_analysis",
            },
            {
                "id": "planner_scale_up",
                "cycle": 0,
                "stage": "main",
                "type": "planning",
                "status": "pending",
                "blocked_by": critic_id,
            },
        ],
    )
    write_state(tmp_path, "pilot_result_analysis", 0)
    write_critic(tmp_path, critic_id, 0, verdict="finish_ok", receipt=False)

    proc = run(tmp_path, "after-critic", "--unit", critic_id)

    assert proc.returncode != 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["reason"] == "critic_receipt_invalid"
    assert units_of(tmp_path)[critic_id]["status"] == "running"


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_identity",
        "missing_secondary",
        "artifact_hash",
        "wrong_cycle",
        "response_hash",
        "consensus",
        "secondary_skipped",
    ],
)
def test_invalid_critic_producer_receipt_cannot_advance(
    tmp_path: Path,
    mutation: str,
) -> None:
    critic_id = "external_critic_after_pilot_result_analysis"
    write_queue(
        tmp_path,
        [
            {
                "id": critic_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "critic",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
                "blocked_by": "pilot_result_analysis",
            }
        ],
    )
    write_state(tmp_path, "pilot_result_analysis", 0)
    write_critic(tmp_path, critic_id, 0, verdict="finish_ok")
    rewrite_last_receipt(tmp_path, mutation)

    proc = run(tmp_path, "after-critic", "--unit", critic_id)

    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["reason"] == "critic_receipt_invalid"
    assert units_of(tmp_path)[critic_id]["status"] == "running"


def test_a_critic_from_an_older_cycle_cannot_advance_the_current_cycle(tmp_path: Path) -> None:
    critic_id = "external_critic_after_main_result_analysis"
    write_queue(
        tmp_path,
        [
            {
                "id": critic_id,
                "cycle": 0,
                "stage": "main",
                "type": "critic",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
                "blocked_by": "main_result_analysis",
            }
        ],
        current_cycle=1,
    )
    write_state(tmp_path, "main_result_analysis", 0)
    write_critic(tmp_path, critic_id, 0)

    proc = run(tmp_path, "after-critic", "--unit", critic_id)

    assert proc.returncode != 0, proc.stdout
    assert units_of(tmp_path)[critic_id]["status"] == "running"
    assert queue_of(tmp_path)["current_cycle"] == 1


def test_a_fresh_critic_artifact_for_another_cycle_stays_in_the_same_attempt(
    tmp_path: Path,
) -> None:
    critic_id = "external_critic_after_result_analysis_c1"
    write_queue(
        tmp_path,
        [
            {
                "id": critic_id,
                "cycle": 1,
                "stage": "iteration",
                "type": "critic",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
                "blocked_by": "result_analysis_c1",
            }
        ],
        current_cycle=1,
    )
    write_state(tmp_path, "result_analysis_c1", 1)
    write_critic(tmp_path, critic_id, 2)

    proc = run(tmp_path, "after-critic", "--unit", critic_id)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["status"] == "retry_required"
    assert units_of(tmp_path)[critic_id]["status"] == "running"


def test_a_run_unit_cannot_complete_without_a_terminal_receipt(tmp_path: Path) -> None:
    write_queue(
        tmp_path,
        [
            {
                "id": "runner_rerun_c1",
                "cycle": 1,
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
        current_cycle=1,
    )

    proc = run(
        tmp_path,
        "complete",
        "--unit",
        "runner_rerun_c1",
        "--status",
        "done",
    )

    assert proc.returncode != 0, proc.stdout
    assert units_of(tmp_path)["runner_rerun_c1"]["status"] == "running"


def test_resuming_a_running_unit_preserves_the_receipt_freshness_boundary(
    tmp_path: Path,
) -> None:
    unit_id = "run_pilot_experiment"
    original_start = "2000-01-01T00:00:00+00:00"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "type": "run",
                "status": "running",
                "started_at": original_start,
            }
        ],
    )
    write_run_receipt(tmp_path, unit_id, 0)

    resumed = run(tmp_path, "next-prompt")
    completed = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert units_of(tmp_path)[unit_id]["started_at"] == original_start
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_initial_run_cannot_be_skipped_through_generic_complete(
    tmp_path: Path,
) -> None:
    unit_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    write_run_receipt(tmp_path, unit_id, 0)

    skipped = run(tmp_path, "complete", "--unit", unit_id, "--status", "skipped")

    assert skipped.returncode == 6, skipped.stdout + skipped.stderr
    assert json.loads(skipped.stdout)["reason"] == "run_skip_requires_adjudication"
    assert units_of(tmp_path)[unit_id]["status"] == "running"


def test_initial_review_cannot_be_skipped_through_generic_complete(
    tmp_path: Path,
) -> None:
    unit_id = "code_review"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "type": "review",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )

    skipped = run(tmp_path, "complete", "--unit", unit_id, "--status", "skipped")

    assert skipped.returncode == 6, skipped.stdout + skipped.stderr
    assert json.loads(skipped.stdout)["reason"] == "review_skip_requires_evidence"
    assert units_of(tmp_path)[unit_id]["status"] == "running"


def test_verify_close_rejects_an_initial_run_without_skip_provenance(
    tmp_path: Path,
) -> None:
    write_queue(
        tmp_path,
        [
            {
                "id": "run_pilot_experiment",
                "cycle": 0,
                "type": "run",
                "status": "skipped",
                "ended_at": "2026-08-15T00:00:00+00:00",
            },
            {
                "id": "close_if_done",
                "cycle": 0,
                "type": "close",
                "status": "done",
                "ended_at": "2026-08-15T00:01:00+00:00",
            },
        ],
    )

    verified = run(tmp_path, "verify-close")

    assert verified.returncode != 0, verified.stdout
    assert "cycle 0 的 skipped 单元没有结构化 skip record" in verified.stdout


def test_verify_close_rejects_a_failed_required_unit(tmp_path: Path) -> None:
    """Failed is evidence of an unfinished run, not a successful dependency or close state."""
    write_queue(
        tmp_path,
        [
            {
                "id": "run_main_experiment",
                "cycle": 0,
                "type": "run",
                "status": "failed",
                "ended_at": "2026-08-15T00:00:00+00:00",
            },
            {
                "id": "close_if_done",
                "cycle": 0,
                "type": "close",
                "status": "done",
                "ended_at": "2026-08-15T00:01:00+00:00",
            },
        ],
    )

    verified = run(tmp_path, "verify-close")

    assert verified.returncode != 0, verified.stdout
    assert "required unit run_main_experiment 处于 failed" in verified.stdout


def test_reclaiming_a_pending_unit_refreshes_its_start_time(tmp_path: Path) -> None:
    old_start = "2000-01-01T00:00:00+00:00"
    write_queue(
        tmp_path,
        [
            {
                "id": "run_pilot_experiment",
                "cycle": 0,
                "type": "run",
                "status": "pending",
                "started_at": old_start,
            }
        ],
    )

    selected = run(tmp_path, "next-prompt")
    unit = units_of(tmp_path)["run_pilot_experiment"]

    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert unit["status"] == "running"
    assert unit["started_at"] != old_start
    assert datetime.fromisoformat(unit["started_at"])


def test_valid_run_receipt_allows_completion_and_later_artifact_drift_is_rejected(
    tmp_path: Path,
) -> None:
    unit_id = "runner_rerun_c1"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 1,
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            },
            {
                "id": "close_if_done",
                "cycle": 1,
                "type": "close",
                "status": "pending",
                "blocked_by": unit_id,
            },
        ],
        current_cycle=1,
    )
    raw = write_run_receipt(tmp_path, unit_id, 1)

    completed = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")
    closed = run(tmp_path, "complete", "--unit", "close_if_done", "--status", "done")
    verified = run(tmp_path, "verify-close")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert closed.returncode == 0, closed.stdout + closed.stderr
    assert verified.returncode == 0, verified.stdout + verified.stderr

    raw.write_text("exit_code=0\nmutated=true\n", encoding="utf-8")
    drifted = run(tmp_path, "verify-close")

    assert drifted.returncode != 0, drifted.stdout
    assert "run artifact" in drifted.stdout


def test_pilot_receipt_cannot_hide_main_artifacts_written_by_the_same_command(
    tmp_path: Path,
) -> None:
    unit_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            },
            {
                "id": "run_main_experiment",
                "cycle": 0,
                "stage": "main",
                "type": "run",
                "status": "pending",
                "blocked_by": unit_id,
            },
        ],
    )
    write_run_receipt(tmp_path, unit_id, 0)
    foreign = tmp_path / "results" / "run_artifacts" / "main" / "observation.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_text('{"stage":"main"}\n', encoding="utf-8")

    completed = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert completed.returncode == 4, completed.stdout + completed.stderr
    assert "旁路 run artifact" in completed.stdout
    assert units_of(tmp_path)[unit_id]["status"] == "running"


@pytest.mark.parametrize("mutation", ["wrong_stage", "missing_stage_arg", "system_python"])
def test_run_receipt_must_bind_stage_scoped_project_environment_execution(
    tmp_path: Path,
    mutation: str,
) -> None:
    unit_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "stage": "pilot",
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    kwargs = {}
    if mutation == "wrong_stage":
        kwargs["execution_stage"] = "main"
    elif mutation == "missing_stage_arg":
        kwargs["execution_argv"] = [
            ".venv/bin/python",
            "code/main.py",
            "--artifact-dir",
            f"results/run_artifacts/{unit_id}",
            "--run-log",
            "results/run.log",
        ]
    else:
        kwargs["execution_environment"] = {
            "kind": "system",
            "prefix": "/usr",
            "python": sys.executable,
            "python_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        }
    write_run_receipt(tmp_path, unit_id, 0, **kwargs)

    completed = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert completed.returncode == 4, completed.stdout + completed.stderr
    assert "run execution" in completed.stdout
    assert units_of(tmp_path)[unit_id]["status"] == "running"


def test_run_receipt_rejects_a_summary_outside_the_immutable_artifact_set(
    tmp_path: Path,
) -> None:
    unit_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    write_run_receipt(tmp_path, unit_id, 0)
    receipt_path = tmp_path / "results" / "run_receipts" / f"{unit_id}.json"
    receipt = json.loads(receipt_path.read_text())
    shared_summary = tmp_path / "results" / "summary.md"
    shared_summary.write_text("# Mutable summary\n", encoding="utf-8")
    receipt["artifacts"] = receipt["artifacts"][:1]
    receipt["summary"] = {
        "path": str(shared_summary.relative_to(tmp_path)),
        "sha256": sha256(shared_summary),
    }
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    proc = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert proc.returncode != 0, proc.stdout
    assert units_of(tmp_path)[unit_id]["status"] == "running"


def test_run_receipt_must_enumerate_every_immutable_artifact(tmp_path: Path) -> None:
    unit_id = "run_main_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "type": "run",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    write_run_receipt(tmp_path, unit_id, 0)
    omitted = (
        tmp_path
        / "results"
        / "run_artifacts"
        / unit_id
        / "observation-15.json"
    )
    omitted.write_text('{"metric": 15}\n', encoding="utf-8")

    completed = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert completed.returncode != 0, completed.stdout
    assert "run receipt 漏列 immutable artifact" in completed.stdout
    assert "observation-15.json" in completed.stdout
    assert units_of(tmp_path)[unit_id]["status"] == "running"


def test_verify_close_rejects_broad_destructive_project_permissions(
    tmp_path: Path,
) -> None:
    write_queue(
        tmp_path,
        [
            {
                "id": "close_if_done",
                "cycle": 0,
                "type": "close",
                "status": "done",
                "ended_at": "2026-08-15T00:01:00+00:00",
            }
        ],
    )
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(rm -rf *)"]}}),
        encoding="utf-8",
    )

    verified = run(tmp_path, "verify-close")

    assert verified.returncode != 0, verified.stdout
    assert "项目级 Claude 权限包含危险 Bash 放行" in verified.stdout
    assert "Bash(rm -rf *)" in verified.stdout


def test_a_review_unit_cannot_complete_with_an_old_or_unbound_report(tmp_path: Path) -> None:
    started_at = "2026-08-14T09:48:10+00:00"
    write_queue(
        tmp_path,
        [
            {
                "id": "review_iteration_c1",
                "cycle": 1,
                "type": "review",
                "status": "running",
                "started_at": started_at,
            }
        ],
        current_cycle=1,
    )
    review = tmp_path / "review.md"
    review.write_text(
        "---\n"
        "blockers_count: 0\n"
        "warnings_count: 0\n"
        "files_reviewed: 4\n"
        "reviewer: gemini-mcp-tool\n"
        "model: stale-model\n"
        "---\n",
        encoding="utf-8",
    )
    stale = datetime.fromisoformat(started_at).timestamp() - 60
    os.utime(review, (stale, stale))

    proc = run(
        tmp_path,
        "complete",
        "--unit",
        "review_iteration_c1",
        "--status",
        "done",
    )

    assert proc.returncode != 0, proc.stdout
    assert units_of(tmp_path)["review_iteration_c1"]["status"] == "running"


def test_a_fresh_cycle_bound_review_with_route_provenance_can_complete(tmp_path: Path) -> None:
    unit_id = "review_iteration_c1"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 1,
                "type": "review",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
        current_cycle=1,
    )
    (tmp_path / "review.md").write_text(
        "---\n"
        f"unit: {unit_id}\n"
        "cycle: 1\n"
        "blockers_count: 0\n"
        "reviewer: gemini-mcp-tool\n"
        "model: azure-deepseek-v4-pro\n"
        "model_identity: deepseek-v4-pro\n"
        "---\n\n# Review\n",
        encoding="utf-8",
    )
    write_review_receipt(tmp_path, unit_id, 1)

    proc = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert units_of(tmp_path)[unit_id]["status"] == "done"
    event = json.loads((tmp_path / "workflow_events.jsonl").read_text().splitlines()[-1])
    assert event["evidence"]["model"] == "azure-deepseek-v4-pro"
    assert event["evidence"]["model_identity"] == "deepseek-v4-pro"
    assert event["evidence"]["producer_receipt"]["artifact_sha256"] == sha256(
        tmp_path / "review.md"
    )


@pytest.mark.parametrize(
    ("unit_id", "cycle"),
    [
        ("code_review", 0),
        ("review_main_experiment", 0),
        ("review_iteration_c1", 1),
    ],
)
def test_a_queue_only_review_report_cannot_claim_mcp_provenance(
    tmp_path: Path,
    unit_id: str,
    cycle: int,
) -> None:
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": cycle,
                "type": "review",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
        current_cycle=cycle,
    )
    (tmp_path / "review.md").write_text(
        "---\n"
        f"unit: {unit_id}\n"
        f"cycle: {cycle}\n"
        "blockers_count: 0\n"
        "reviewer: cycle-1-review-skipped\n"
        "model: coordinator-claimed-model\n"
        "model_identity: coordinator-claimed-identity\n"
        "---\n\n# Review\n",
        encoding="utf-8",
    )

    proc = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert proc.returncode != 0, proc.stdout
    assert "producer receipt" in proc.stdout
    assert units_of(tmp_path)[unit_id]["status"] == "running"


def test_record_review_report_rejects_a_non_mcp_parent(tmp_path: Path) -> None:
    unit_id = "code_review"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "type": "review",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    report = (
        "---\n"
        f"unit: {unit_id}\n"
        "cycle: 0\n"
        "blockers_count: 0\n"
        "reviewer: gemini-mcp-tool\n"
        "model: route-a\n"
        "model_identity: identity-a\n"
        "---\n\n# Review\n"
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(ENGINE),
            "record-review-report",
            "--project-root",
            str(tmp_path),
            "--unit",
            unit_id,
            "--cycle",
            "0",
        ],
        input=report,
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0, proc.stdout
    assert "producer" in proc.stdout
    assert not (tmp_path / "review.md").exists()


@pytest.mark.parametrize("mutation", ["artifact_hash", "wrong_cycle", "model_identity"])
def test_review_completion_rejects_a_drifted_producer_receipt(
    tmp_path: Path,
    mutation: str,
) -> None:
    unit_id = "review_main_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "type": "review",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    (tmp_path / "review.md").write_text(
        "---\n"
        f"unit: {unit_id}\n"
        "cycle: 0\n"
        "blockers_count: 0\n"
        "reviewer: gemini-mcp-tool\n"
        "model: route-a\n"
        "model_identity: identity-a\n"
        "---\n\n# Review\n",
        encoding="utf-8",
    )
    write_review_receipt(tmp_path, unit_id, 0)
    rewrite_review_receipt(tmp_path, mutation)

    proc = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert proc.returncode != 0, proc.stdout
    assert "producer receipt" in proc.stdout
    assert units_of(tmp_path)[unit_id]["status"] == "running"


def test_a_late_review_overwrite_blocks_the_dependent_run(tmp_path: Path) -> None:
    review_id = "code_review"
    run_id = "run_pilot_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": review_id,
                "cycle": 0,
                "type": "review",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            },
            {
                "id": run_id,
                "cycle": 0,
                "type": "run",
                "status": "pending",
                "blocked_by": review_id,
            },
        ],
    )
    review = tmp_path / "review.md"
    review.write_text(
        "---\n"
        f"unit: {review_id}\n"
        "cycle: 0\n"
        "blockers_count: 0\n"
        "reviewer: gemini-mcp-tool\n"
        "model: azure-deepseek-v4-pro\n"
        "model_identity: deepseek-v4-pro\n"
        "---\n",
        encoding="utf-8",
    )
    write_review_receipt(tmp_path, review_id, 0)
    assert run(tmp_path, "complete", "--unit", review_id, "--status", "done").returncode == 0
    assert run(tmp_path, "next-prompt").returncode == 0
    review.write_text(review.read_text().replace("blockers_count: 0", "blockers_count: 1"))
    write_run_receipt(tmp_path, run_id, 0)

    proc = run(tmp_path, "complete", "--unit", run_id, "--status", "done")

    assert proc.returncode != 0, proc.stdout
    assert "review" in proc.stdout.lower()
    assert units_of(tmp_path)[run_id]["status"] == "running"


def test_review_frontmatter_cannot_hide_a_body_blocker(tmp_path: Path) -> None:
    unit_id = "review_main_experiment"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "type": "review",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    (tmp_path / "review.md").write_text(
        "---\n"
        f"unit: {unit_id}\n"
        "cycle: 0\n"
        "blockers_count: 0\n"
        "reviewer: gemini-mcp-tool\n"
        "model: route-a\n"
        "model_identity: identity-a\n"
        "---\n\n"
        "# Review\n\n"
        "## Blockers (must fix before running)\n"
        "None.\n\n"
        "## Overall\n"
        "Approved.\n\n"
        "- [B3] <severity:high> scripts/main.py:main: executor is incomplete\n",
        encoding="utf-8",
    )

    completed = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert completed.returncode != 0, completed.stdout
    assert "正文" in completed.stdout
    assert units_of(tmp_path)[unit_id]["status"] == "running"


def test_a_review_with_an_invalid_unit_start_time_fails_closed(tmp_path: Path) -> None:
    unit_id = "code_review"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "type": "review",
                "status": "running",
                "started_at": "not-an-iso-time",
            }
        ],
    )
    (tmp_path / "review.md").write_text(
        "---\n"
        f"unit: {unit_id}\n"
        "cycle: 0\n"
        "blockers_count: 0\n"
        "reviewer: gemini-mcp-tool\n"
        "model: azure-deepseek-v4-pro\n"
        "model_identity: deepseek-v4-pro\n"
        "---\n",
        encoding="utf-8",
    )

    proc = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")

    assert proc.returncode != 0, proc.stdout
    assert units_of(tmp_path)[unit_id]["status"] == "running"


def test_a_blocked_unit_prevents_close(tmp_path: Path) -> None:
    write_queue(
        tmp_path,
        [
            {
                "id": "run_pilot_experiment",
                "cycle": 0,
                "type": "run",
                "status": "blocked",
            },
            {
                "id": "close_if_done",
                "cycle": 0,
                "type": "close",
                "status": "pending",
                "blocked_by": "run_pilot_experiment",
            },
        ],
    )

    proc = run(tmp_path, "complete", "--unit", "close_if_done", "--status", "done")

    assert proc.returncode != 0, proc.stdout
    assert units_of(tmp_path)["close_if_done"]["status"] == "pending"


def test_verify_close_rejects_a_tampered_engine_event_chain(tmp_path: Path) -> None:
    unit_id = "plan_gate"
    write_queue(
        tmp_path,
        [
            {
                "id": unit_id,
                "cycle": 0,
                "type": "planning",
                "status": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )
    completed = run(tmp_path, "complete", "--unit", unit_id, "--status", "done")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    events = tmp_path / "workflow_events.jsonl"
    events.write_text(events.read_text().replace('"status": "done"', '"status": "failed"'))

    proc = run(tmp_path, "verify-close")

    assert proc.returncode != 0, proc.stdout
    assert "hash" in proc.stdout.lower()
