#!/usr/bin/env python3
"""Record what a comparison run actually ran on, at start and at finish.

The first three #108 runs had their manifests reconstructed after the fact, so
commit_at_capture degraded to "see git log" and the wire pointers to globs
(review 2026-08-12 P1). Each supervisor invocation records its own provenance
in an exclusive attempt file. The exact path printed by `start` must be passed
to `finish`; neither command silently falls back to the legacy manifest.

    python3 scripts/ar_run_manifest.py start  --project-root <dir> --runner "<cmd>"
    python3 scripts/ar_run_manifest.py finish --project-root <dir> --attempt <file>
    python3 scripts/ar_run_manifest.py verify --project-root <dir> [--attempt <file>]

Secrets never enter the manifest: only key NAMES for credentials, values only
for model names and base URLs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# env keys whose VALUES are safe and load-bearing for reproduction
VALUE_KEYS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "GEMINI_BASE_URL", "GEMINI_REVIEW_MODEL", "GEMINI_CRITIC_MODEL",
    "GPT_CRITIC_BASE_URL", "GPT_CRITIC_MODEL",
)


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              cwd=REPO).stdout.strip()
    except Exception as exc:  # noqa: BLE001 - provenance must not kill the run
        return f"<unavailable: {type(exc).__name__}>"


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def manifest_path(root: Path) -> Path:
    return root / "run_manifest.json"


def attempt_path(root: Path, value: str) -> Path:
    """Resolve a CLI attempt path, with relative paths based at the run root."""
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def print_error(code: str, message: str) -> None:
    print(json.dumps({"status": "error", "code": code, "message": message}),
          file=sys.stderr)


def _effective_env() -> dict[str, str]:
    """Managed values live in the projection files, not the launcher shell.

    The first sentinel recorded env_values as empty because start() read
    os.environ only. Explicit exports still win; the projection is the
    fallback, tried for whichever consumer exists next to the run.
    """
    import os
    merged: dict[str, str] = {}
    for side in ("ar-runtime",):
        f = REPO / side / ".claude" / "settings.local.json"
        if f.exists():
            try:
                for k, v in json.loads(f.read_text()).get("env", {}).items():
                    if isinstance(v, str):
                        merged.setdefault(k, v)
            except ValueError:
                pass
    for k in VALUE_KEYS:
        if os.environ.get(k):
            merged[k] = os.environ[k]
    return merged


def cmd_start(args: argparse.Namespace) -> None:
    root = Path(args.project_root).resolve()
    if (
        len(args.attempt_id) > 128
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.attempt_id) is None
    ):
        print_error(
            "invalid_attempt_id",
            "attempt id must be 1-128 ASCII letters, digits, dots, underscores, or hyphens",
        )
        raise SystemExit(2)
    root.mkdir(parents=True, exist_ok=True)
    # 脏文件不只记路径：路径证明不了实跑正文改了什么（review 2026-08-13 P0）。
    # 逐文件哈希 + 相对 HEAD 的 patch，事后可精确复核实跑用的每一行。
    dirty_lines = sh(["git", "status", "--porcelain"]).splitlines()
    dirty_files = [ln[3:] for ln in dirty_lines
                   if ln[:2].strip() and not ln.startswith("??")]
    dirty_sha = {}
    for rel in dirty_files:
        f = REPO / rel
        if f.is_file():
            dirty_sha[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    doc = {
        "run": root.name,
        "attempt_id": args.attempt_id,
        "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit_at_capture": sh(["git", "rev-parse", "HEAD"]),
        "engine_sha256": digest(REPO / "ar-runtime" / "scripts" / "ar-workflow-engine.py"),
        "idea_file": args.idea or None,
        "idea_sha256": digest(Path(args.idea)) if args.idea else None,
        "worktree_dirty": dirty_lines,
        "dirty_file_sha256": dirty_sha,
        "dirty_patch": sh(["git", "diff"]) or None,
        "runner_cmd": args.runner,
        "runner_version": sh(args.runner.split()[:1] + ["--version"]) if args.runner else None,
        "bun_version": sh(["bun", "--version"]),
        "python_version": sys.version.split()[0],
        "env_values": {k: v for k, v in _effective_env().items() if k in VALUE_KEYS},
        "config_sha256": {
            "config/providers.example.json": digest(REPO / "config" / "providers.example.json"),
            "config/providers.local.json": digest(REPO / "config" / "providers.local.json"),
        },
    }
    # 每次 supervisor invocation 一个不可覆盖的 attempt 文件。曾经 start 无条件重写
    # run_manifest.json，事后补跑一次 supervisor 就把起跑现场覆盖成了 checkout 时刻
    # ——归档里的假起跑时间就是这样来的。attempt 文件独占创建，撞名直接失败；
    # run_manifest.json 只归第一个 attempt，之后的 start 不碰它。
    body = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    attempt_file = root / f"run_manifest.attempt-{args.attempt_id}.json"
    with attempt_file.open("x") as f:
        f.write(body)
    legacy = manifest_path(root)
    if not legacy.exists():
        legacy.write_text(body)
    print(f"run manifest started: {attempt_file}")


def cmd_finish(args: argparse.Namespace) -> None:
    root = Path(args.project_root).resolve()
    if not args.attempt:
        print_error(
            "attempt_required",
            "finish requires --attempt; refusing to create or overwrite run_manifest.json",
        )
        raise SystemExit(2)
    path = attempt_path(root, args.attempt)
    if not path.is_relative_to(root):
        print_error("attempt_outside_project", f"attempt file must be inside {root}: {path}")
        raise SystemExit(2)
    if not path.is_file():
        print_error("attempt_missing", f"attempt file does not exist: {path}")
        raise SystemExit(2)
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print_error("attempt_invalid", f"cannot read attempt manifest {path}: {exc}")
        raise SystemExit(2) from None
    if not isinstance(doc, dict):
        print_error("attempt_invalid", f"attempt manifest must contain a JSON object: {path}")
        raise SystemExit(2)
    # runner 每次给虚拟环境起的名字都不一样（.venv / .conda-env / .python-venv /
    # .python-env 都见过），按名字排除是打地鼠。按证据排除：pyvenv.cfg 或 conda-meta
    # 标记的目录整棵跳过。
    env_roots = {c.parent for c in root.rglob("pyvenv.cfg")}
    env_roots |= {c.parent for c in root.rglob("conda-meta") if c.is_dir()}
    evidence_files: list[tuple[str, Path]] = []
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root).as_posix()
        if any(parent in env_roots for parent in f.parents):
            continue
        if f.is_symlink():
            print_error(
                "evidence_symlink",
                f"refusing to archive symlink from the project evidence tree: {rel}",
            )
            raise SystemExit(2)
        if not f.is_file():
            continue
        if ".state" in f.parts or "__pycache__" in f.parts:
            continue
        if rel == "run_manifest.json" or rel.startswith("run_manifest.attempt-") \
                or rel.startswith(".run_manifest.attempt-") \
                or rel.endswith((".lock", ".pyc")):
            continue
        evidence_files.append((rel, f))

    snapshot = path.with_suffix(".snapshot")
    if snapshot.exists():
        print_error("attempt_snapshot_exists", f"attempt snapshot already exists: {snapshot}")
        raise SystemExit(2)
    temporary = Path(tempfile.mkdtemp(prefix=f".{snapshot.name}-", dir=root))
    published = False
    try:
        hashes = {}
        for rel, source in evidence_files:
            target = temporary / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            hashes[rel] = hashlib.sha256(target.read_bytes()).hexdigest()
        temporary.replace(snapshot)
        published = True
        doc["finished_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if args.exit_code is not None:
            doc["supervisor_exit_code"] = args.exit_code
        doc["snapshot"] = snapshot.name
        doc["sha256"] = hashes
        manifest_tmp = path.with_suffix(path.suffix + ".tmp")
        manifest_tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        manifest_tmp.replace(path)
    except Exception:
        if published:
            shutil.rmtree(snapshot, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    print(f"run manifest finished: {len(hashes)} files hashed")


def manifest_label(root: Path, path: Path) -> str:
    return path.name if path.parent == root else str(path)


def verify_manifest(root: Path, path: Path) -> dict[str, object]:
    """Verify one manifest without letting malformed evidence abort the batch."""
    label = manifest_label(root, path)
    if not path.is_file():
        return {
            "attempt": label,
            "status": "missing_manifest",
            "error": f"manifest file does not exist: {path}",
        }
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "attempt": label,
            "status": "invalid_manifest",
            "error": f"manifest is not valid JSON: {exc}",
        }
    if not isinstance(doc, dict):
        return {
            "attempt": label,
            "status": "invalid_manifest",
            "error": "manifest must contain a JSON object",
        }
    hashes = doc.get("sha256")
    if hashes is None:
        return {"attempt": label, "status": "never_finished"}
    if not isinstance(hashes, dict):
        return {
            "attempt": label,
            "status": "invalid_manifest",
            "error": "sha256 must be a JSON object",
        }
    if not hashes:
        return {"attempt": label, "status": "never_finished"}
    evidence_root = root
    snapshot_value = doc.get("snapshot")
    if snapshot_value is not None:
        if not isinstance(snapshot_value, str) or Path(snapshot_value).name != snapshot_value:
            return {
                "attempt": label,
                "status": "invalid_manifest",
                "error": "snapshot must be one directory name inside the project root",
            }
        snapshot_path = root / snapshot_value
        if snapshot_path.is_symlink():
            return {
                "attempt": label,
                "status": "invalid_manifest",
                "error": f"snapshot must not be a symlink: {snapshot_value}",
            }
        evidence_root = snapshot_path.resolve()
        if not evidence_root.is_relative_to(root):
            return {
                "attempt": label,
                "status": "invalid_manifest",
                "error": f"snapshot escapes the project root: {snapshot_value}",
            }
        if not evidence_root.is_dir():
            return {
                "attempt": label,
                "status": "missing_snapshot",
                "error": f"attempt snapshot does not exist: {snapshot_value}",
            }
    mismatch, missing = [], []
    for rel, want in hashes.items():
        if (
            not isinstance(rel, str)
            or not isinstance(want, str)
            or re.fullmatch(r"[0-9a-f]{64}", want) is None
        ):
            return {
                "attempt": label,
                "status": "invalid_manifest",
                "error": "sha256 entries must map relative path strings to lowercase SHA256 digests",
            }
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts:
            return {
                "attempt": label,
                "status": "invalid_manifest",
                "error": f"sha256 path escapes evidence root: {rel}",
            }
        unresolved = evidence_root
        traverses_symlink = False
        for part in relative.parts:
            unresolved /= part
            if unresolved.is_symlink():
                traverses_symlink = True
                break
        if traverses_symlink:
            return {
                "attempt": label,
                "status": "invalid_manifest",
                "error": f"sha256 path traverses a symlink: {rel}",
            }
        f = unresolved.resolve()
        if not f.is_relative_to(evidence_root):
            return {
                "attempt": label,
                "status": "invalid_manifest",
                "error": f"sha256 path escapes evidence root: {rel}",
            }
        if not f.is_file():
            missing.append(rel)
            continue
        try:
            actual = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError as exc:
            return {
                "attempt": label,
                "status": "verification_error",
                "error": f"cannot read artifact {rel}: {exc}",
            }
        if actual != want:
            mismatch.append(rel)
    return {
        "attempt": label,
        "status": "verified" if not mismatch and not missing else "drifted",
        "ok": len(hashes) - len(mismatch) - len(missing),
        "mismatch": mismatch,
        "missing": missing,
    }


def cmd_verify(args: argparse.Namespace) -> None:
    """Verify one attempt or every per-invocation manifest in an evidence package."""
    root = Path(args.project_root).resolve()
    if args.attempt:
        paths = [attempt_path(root, args.attempt)]
    else:
        paths = sorted(root.glob("run_manifest.attempt-*.json"))
        if not paths and manifest_path(root).exists():
            paths = [manifest_path(root)]
    if not paths:
        verdict = {
            "status": "failed",
            "attempts": [],
            "error": f"no run manifests found under {root}",
        }
        print(json.dumps(verdict, ensure_ascii=False))
        raise SystemExit(2)

    attempts = [verify_manifest(root, path) for path in paths]
    statuses = {attempt["status"] for attempt in attempts}
    verdict = {
        "status": "verified" if statuses == {"verified"} else "failed",
        "attempts": attempts,
    }
    print(json.dumps(verdict, ensure_ascii=False))
    if verdict["status"] == "verified":
        raise SystemExit(0)
    invalid = {
        "missing_manifest", "missing_snapshot", "invalid_manifest", "never_finished",
        "verification_error",
    }
    raise SystemExit(2 if statuses & invalid else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--project-root", required=True)
    s.add_argument("--runner", default="")
    s.add_argument("--idea", default="")
    s.add_argument("--attempt-id",
                   default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"),
                   help="Unique per supervisor invocation; the attempt file refuses to overwrite")
    s.set_defaults(func=cmd_start)
    v = sub.add_parser("verify")
    v.add_argument("--project-root", required=True)
    v.add_argument("--attempt", default="",
                   help="Verify one explicit attempt file instead of every attempt")
    v.set_defaults(func=cmd_verify)
    f = sub.add_parser("finish")
    f.add_argument("--project-root", required=True)
    f.add_argument("--attempt", default="",
                   help="Attempt file written by start; finish only completes that attempt")
    f.add_argument("--exit-code", default=None, type=int,
                   help="Supervisor shell exit code, recorded as supervisor_exit_code")
    f.set_defaults(func=cmd_finish)
    args = parser.parse_args()
    try:
        args.func(args)
    except OSError as exc:
        print_error("io_error", f"{args.cmd} failed: {exc}")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
