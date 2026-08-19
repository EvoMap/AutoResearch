"""Contracts for producing and verifying per-invocation run manifests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ar_run_manifest.py"


def run_manifest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def write_manifest(root: Path, name: str, hashes: dict[str, str] | None) -> Path:
    path = root / name
    doc: dict[str, object] = {"run": root.name, "attempt_id": name}
    if hashes is not None:
        doc["sha256"] = hashes
    path.write_text(json.dumps(doc))
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_start_finish_and_default_verify_use_the_attempt_file(tmp_path: Path) -> None:
    proc = run_manifest(
        "start",
        "--project-root",
        str(tmp_path),
        "--attempt-id",
        "direct-contract",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    attempt = Path(proc.stdout.removeprefix("run manifest started: ").strip())
    artifact = tmp_path / "result.txt"
    artifact.write_text("terminal evidence")

    finish = run_manifest(
        "finish",
        "--project-root",
        str(tmp_path),
        "--attempt",
        str(attempt),
        "--exit-code",
        "0",
    )
    verify = run_manifest("verify", "--project-root", str(tmp_path))

    assert finish.returncode == 0, finish.stdout + finish.stderr
    assert verify.returncode == 0, verify.stdout + verify.stderr
    doc = json.loads(attempt.read_text())
    assert doc["supervisor_exit_code"] == 0
    assert doc["sha256"][artifact.name] == sha256(artifact)
    assert json.loads(verify.stdout)["attempts"][0]["attempt"] == attempt.name


def test_failed_attempt_then_recovery_keeps_every_attempt_verifiable(
    tmp_path: Path,
) -> None:
    first_start = run_manifest(
        "start",
        "--project-root",
        str(tmp_path),
        "--attempt-id",
        "failed",
    )
    assert first_start.returncode == 0, first_start.stdout + first_start.stderr
    first_attempt = Path(
        first_start.stdout.removeprefix("run manifest started: ").strip()
    )
    state = tmp_path / "state.md"
    state.write_text("attempt: failed\n", encoding="utf-8")
    first_finish = run_manifest(
        "finish",
        "--project-root",
        str(tmp_path),
        "--attempt",
        str(first_attempt),
        "--exit-code",
        "2",
    )
    assert first_finish.returncode == 0, first_finish.stdout + first_finish.stderr

    state.write_text("attempt: recovered\n", encoding="utf-8")
    second_start = run_manifest(
        "start",
        "--project-root",
        str(tmp_path),
        "--attempt-id",
        "recovered",
    )
    assert second_start.returncode == 0, second_start.stdout + second_start.stderr
    second_attempt = Path(
        second_start.stdout.removeprefix("run manifest started: ").strip()
    )
    second_finish = run_manifest(
        "finish",
        "--project-root",
        str(tmp_path),
        "--attempt",
        str(second_attempt),
        "--exit-code",
        "0",
    )
    assert second_finish.returncode == 0, second_finish.stdout + second_finish.stderr

    verify = run_manifest("verify", "--project-root", str(tmp_path))

    assert verify.returncode == 0, verify.stdout + verify.stderr
    verdict = json.loads(verify.stdout)
    assert [item["status"] for item in verdict["attempts"]] == [
        "verified",
        "verified",
    ]
    assert json.loads(first_attempt.read_text())["snapshot"]
    assert json.loads(second_attempt.read_text())["snapshot"]

    state.write_text("live project changed after both attempts\n", encoding="utf-8")
    live_drift = run_manifest("verify", "--project-root", str(tmp_path))
    assert live_drift.returncode == 0, live_drift.stdout + live_drift.stderr

    first_doc = json.loads(first_attempt.read_text())
    first_snapshot = tmp_path / first_doc["snapshot"]
    (first_snapshot / "state.md").write_text(
        "sealed evidence was tampered with\n", encoding="utf-8"
    )
    snapshot_drift = run_manifest("verify", "--project-root", str(tmp_path))
    assert snapshot_drift.returncode == 1, snapshot_drift.stdout + snapshot_drift.stderr
    drift_verdict = json.loads(snapshot_drift.stdout)
    assert [item["status"] for item in drift_verdict["attempts"]] == [
        "drifted",
        "verified",
    ]
    assert drift_verdict["attempts"][0]["mismatch"] == ["state.md"]


def test_verify_fails_closed_when_an_attempt_snapshot_is_missing(tmp_path: Path) -> None:
    started = run_manifest(
        "start",
        "--project-root",
        str(tmp_path),
        "--attempt-id",
        "missing-snapshot",
    )
    assert started.returncode == 0, started.stdout + started.stderr
    attempt = Path(started.stdout.removeprefix("run manifest started: ").strip())
    (tmp_path / "state.md").write_text("sealed\n", encoding="utf-8")
    finished = run_manifest(
        "finish",
        "--project-root",
        str(tmp_path),
        "--attempt",
        str(attempt),
        "--exit-code",
        "0",
    )
    assert finished.returncode == 0, finished.stdout + finished.stderr
    snapshot = tmp_path / json.loads(attempt.read_text())["snapshot"]
    for artifact in snapshot.rglob("*"):
        if artifact.is_file():
            artifact.unlink()
    snapshot.rmdir()

    verify = run_manifest("verify", "--project-root", str(tmp_path))

    assert verify.returncode == 2, verify.stdout + verify.stderr
    verdict = json.loads(verify.stdout)
    assert verdict["attempts"][0]["status"] == "missing_snapshot"


def test_finish_excludes_a_runtime_environment_before_rejecting_symlinks(
    tmp_path: Path,
) -> None:
    started = run_manifest(
        "start",
        "--project-root",
        str(tmp_path),
        "--attempt-id",
        "runtime-environment",
    )
    assert started.returncode == 0, started.stdout + started.stderr
    attempt = Path(started.stdout.removeprefix("run manifest started: ").strip())

    artifact = tmp_path / "state.md"
    artifact.write_text("terminal evidence\n", encoding="utf-8")
    environment = tmp_path / ".venv"
    environment.joinpath("bin").mkdir(parents=True)
    environment.joinpath("pyvenv.cfg").write_text(
        "home = /usr/bin\n",
        encoding="utf-8",
    )
    interpreter = tmp_path.parent / f"{tmp_path.name}-python"
    interpreter.write_text("external interpreter\n", encoding="utf-8")
    environment.joinpath("bin", "python").symlink_to(interpreter)

    finished = run_manifest(
        "finish",
        "--project-root",
        str(tmp_path),
        "--attempt",
        str(attempt),
        "--exit-code",
        "0",
    )

    assert finished.returncode == 0, finished.stdout + finished.stderr
    doc = json.loads(attempt.read_text())
    assert doc["sha256"][artifact.name] == sha256(artifact)
    assert not any(path.startswith(".venv/") for path in doc["sha256"])
    snapshot = tmp_path / doc["snapshot"]
    assert not snapshot.joinpath(".venv").exists()
    verified = run_manifest("verify", "--project-root", str(tmp_path))
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_finish_rejects_symlink_evidence_without_copying_its_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("must not enter the attempt snapshot\n", encoding="utf-8")
    started = run_manifest(
        "start",
        "--project-root",
        str(tmp_path),
        "--attempt-id",
        "symlink-evidence",
    )
    assert started.returncode == 0, started.stdout + started.stderr
    attempt = Path(started.stdout.removeprefix("run manifest started: ").strip())
    (tmp_path / "linked-secret.txt").symlink_to(outside)

    finished = run_manifest(
        "finish",
        "--project-root",
        str(tmp_path),
        "--attempt",
        str(attempt),
        "--exit-code",
        "0",
    )

    assert finished.returncode == 2
    assert json.loads(finished.stderr)["code"] == "evidence_symlink"
    assert "finished_at_utc" not in json.loads(attempt.read_text())
    assert not list(tmp_path.glob("run_manifest.attempt-*.snapshot"))


def test_verify_rejects_a_snapshot_directory_replaced_by_a_symlink(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    artifact = outside / "state.md"
    artifact.write_text("same bytes\n", encoding="utf-8")
    attempt = write_manifest(
        tmp_path,
        "run_manifest.attempt-linked.json",
        {"state.md": sha256(artifact)},
    )
    linked_snapshot = tmp_path / "run_manifest.attempt-linked.snapshot"
    linked_snapshot.symlink_to(outside, target_is_directory=True)
    doc = json.loads(attempt.read_text())
    doc["snapshot"] = linked_snapshot.name
    attempt.write_text(json.dumps(doc), encoding="utf-8")

    verify = run_manifest("verify", "--project-root", str(tmp_path))

    assert verify.returncode == 2
    verdict = json.loads(verify.stdout)
    assert verdict["attempts"][0]["status"] == "invalid_manifest"
    assert "must not be a symlink" in verdict["attempts"][0]["error"]


def test_start_rejects_an_attempt_id_that_escapes_the_project_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    # The prefixed first component must exist for the kernel to traverse the
    # following `..`; a writable project can satisfy that precondition.
    (root / "run_manifest.attempt-..").mkdir()
    escaped = tmp_path / "escaped-attempt.json"

    proc = run_manifest(
        "start",
        "--project-root",
        str(root),
        "--attempt-id",
        "../../escaped-attempt",
    )

    assert proc.returncode != 0
    error = json.loads(proc.stderr)
    assert error["code"] == "invalid_attempt_id"
    assert not escaped.exists()
    assert not list(root.glob("run_manifest.attempt-*.json"))


def test_verify_accepts_an_explicit_attempt_file(tmp_path: Path) -> None:
    artifact = tmp_path / "result.txt"
    artifact.write_text("finished evidence")
    attempt = write_manifest(
        tmp_path,
        "run_manifest.attempt-one.json",
        {artifact.name: sha256(artifact)},
    )
    write_manifest(tmp_path, "run_manifest.json", None)

    proc = run_manifest(
        "verify", "--project-root", str(tmp_path), "--attempt", str(attempt)
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["status"] == "verified"
    assert verdict["attempts"] == [
        {
            "attempt": attempt.name,
            "status": "verified",
            "ok": 1,
            "mismatch": [],
            "missing": [],
        }
    ]


def test_default_verify_checks_every_attempt_and_reports_each_failure(
    tmp_path: Path,
) -> None:
    good = tmp_path / "good.txt"
    good.write_text("good")
    changed = tmp_path / "changed.txt"
    changed.write_text("changed after finish")
    write_manifest(
        tmp_path,
        "run_manifest.attempt-01-good.json",
        {good.name: sha256(good)},
    )
    write_manifest(tmp_path, "run_manifest.attempt-02-unfinished.json", None)
    write_manifest(
        tmp_path,
        "run_manifest.attempt-03-drifted.json",
        {changed.name: "0" * 64, "missing.txt": "1" * 64},
    )

    proc = run_manifest("verify", "--project-root", str(tmp_path))

    assert proc.returncode != 0
    verdict = json.loads(proc.stdout)
    assert verdict["status"] == "failed"
    assert [item["status"] for item in verdict["attempts"]] == [
        "verified",
        "never_finished",
        "drifted",
    ]
    assert verdict["attempts"][2]["mismatch"] == [changed.name]
    assert verdict["attempts"][2]["missing"] == ["missing.txt"]


def test_unfinished_legacy_does_not_mask_finished_attempts(tmp_path: Path) -> None:
    artifact = tmp_path / "result.txt"
    artifact.write_text("sealed")
    write_manifest(tmp_path, "run_manifest.json", None)
    attempt = write_manifest(
        tmp_path,
        "run_manifest.attempt-finished.json",
        {artifact.name: sha256(artifact)},
    )

    proc = run_manifest("verify", "--project-root", str(tmp_path))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["status"] == "verified"
    assert [item["attempt"] for item in verdict["attempts"]] == [attempt.name]


def test_default_verify_falls_back_to_a_legacy_manifest(tmp_path: Path) -> None:
    artifact = tmp_path / "legacy-result.txt"
    artifact.write_text("legacy evidence")
    write_manifest(
        tmp_path,
        "run_manifest.json",
        {artifact.name: sha256(artifact)},
    )

    proc = run_manifest("verify", "--project-root", str(tmp_path))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["status"] == "verified"
    assert verdict["attempts"][0]["attempt"] == "run_manifest.json"


def test_verify_missing_attempt_is_a_structured_error(tmp_path: Path) -> None:
    missing = tmp_path / "run_manifest.attempt-missing.json"

    proc = run_manifest(
        "verify", "--project-root", str(tmp_path), "--attempt", str(missing)
    )

    assert proc.returncode != 0
    verdict = json.loads(proc.stdout)
    assert verdict["status"] == "failed"
    assert verdict["attempts"] == [
        {
            "attempt": missing.name,
            "status": "missing_manifest",
            "error": f"manifest file does not exist: {missing}",
        }
    ]


def test_verify_without_any_manifest_is_a_structured_error(tmp_path: Path) -> None:
    proc = run_manifest("verify", "--project-root", str(tmp_path))

    assert proc.returncode != 0
    verdict = json.loads(proc.stdout)
    assert verdict["status"] == "failed"
    assert verdict["attempts"] == []
    assert "no run manifests found" in verdict["error"]


def test_verify_invalid_json_is_a_structured_error(tmp_path: Path) -> None:
    attempt = tmp_path / "run_manifest.attempt-invalid.json"
    attempt.write_text("{not-json")

    proc = run_manifest("verify", "--project-root", str(tmp_path))

    assert proc.returncode != 0
    verdict = json.loads(proc.stdout)
    assert verdict["status"] == "failed"
    assert verdict["attempts"][0]["attempt"] == attempt.name
    assert verdict["attempts"][0]["status"] == "invalid_manifest"
    assert "valid JSON" in verdict["attempts"][0]["error"]


def test_verify_rejects_a_malformed_hash_table(tmp_path: Path) -> None:
    attempt = tmp_path / "run_manifest.attempt-invalid-hashes.json"
    attempt.write_text(json.dumps({"sha256": []}))

    proc = run_manifest("verify", "--project-root", str(tmp_path))

    assert proc.returncode != 0
    verdict = json.loads(proc.stdout)
    assert verdict["attempts"][0]["status"] == "invalid_manifest"
    assert verdict["attempts"][0]["error"] == "sha256 must be a JSON object"


def test_finish_requires_an_attempt_and_preserves_legacy(tmp_path: Path) -> None:
    legacy = tmp_path / "run_manifest.json"
    legacy.write_text('{"sentinel": "must stay unchanged"}\n')

    proc = run_manifest("finish", "--project-root", str(tmp_path), "--exit-code", "0")

    assert proc.returncode != 0
    error = json.loads(proc.stderr)
    assert error["status"] == "error"
    assert error["code"] == "attempt_required"
    assert "--attempt" in error["message"]
    assert legacy.read_text() == '{"sentinel": "must stay unchanged"}\n'


def test_finish_missing_attempt_fails_without_creating_legacy(tmp_path: Path) -> None:
    missing = tmp_path / "run_manifest.attempt-missing.json"

    proc = run_manifest(
        "finish",
        "--project-root",
        str(tmp_path),
        "--attempt",
        str(missing),
        "--exit-code",
        "2",
    )

    assert proc.returncode != 0
    error = json.loads(proc.stderr)
    assert error["status"] == "error"
    assert error["code"] == "attempt_missing"
    assert str(missing) in error["message"]
    assert not (tmp_path / "run_manifest.json").exists()


def test_finish_refuses_to_modify_an_attempt_outside_the_project_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "run_manifest.attempt-outside.json"
    original = '{"sentinel": "must stay unchanged"}\n'
    outside.write_text(original)

    proc = run_manifest(
        "finish",
        "--project-root",
        str(root),
        "--attempt",
        str(outside),
        "--exit-code",
        "0",
    )

    assert proc.returncode != 0
    error = json.loads(proc.stderr)
    assert error["code"] == "attempt_outside_project"
    assert outside.read_text() == original


def test_finish_rejects_a_non_integer_exit_code_before_writing(tmp_path: Path) -> None:
    attempt = write_manifest(
        tmp_path,
        "run_manifest.attempt-invalid-exit.json",
        None,
    )
    original = attempt.read_text()

    proc = run_manifest(
        "finish",
        "--project-root",
        str(tmp_path),
        "--attempt",
        str(attempt),
        "--exit-code",
        "not-an-integer",
    )

    assert proc.returncode != 0
    assert "invalid int value" in proc.stderr
    assert attempt.read_text() == original
