#!/usr/bin/env python3
"""
Deterministic AutoResearch workflow queue engine.

This script owns only workflow/cycle mechanics. It does not call agents, review
code, or interpret research content beyond simple state.md fields written by
the coordinator.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


VALID_STATUSES = {"pending", "running", "running-but-incomplete", "done", "failed", "skipped", "blocked"}
ACTIVE_STATUSES = {"pending", "running", "running-but-incomplete"}
TERMINAL_STATUSES = {"done", "failed", "skipped"}
SATISFIED_DEPENDENCY_STATUSES = {"done", "skipped"}

DEFAULT_MAX_CYCLES = 3

# 引擎自己写出去的那份队列的镜像。文件权限不能阻止 coordinator 写队列，
# 所以引擎需要一个独立对照物，才能认出未经授权的直接改写。
ENGINE_MIRROR_NAME = "workflow_queue.engine.json"
ENGINE_EVENTS_NAME = "workflow_events.jsonl"
COMPLETION_AUTHORITY_VERSION = 1
RUN_RECEIPT_SCHEMA_VERSION = 1
CRITIC_RECEIPT_SCHEMA_VERSION = 1
CRITIC_RECEIPT_SOURCE = "ar-external-critic-mcp"
REVIEW_RECEIPT_SCHEMA_VERSION = 1
REVIEW_RECEIPT_SOURCE = "ar-gemini-review-mcp"

# 自组织抢占（self-organizing claim）配置：
# worker 抢占后持有租约；租约过期视为 worker 失联，单元自动回收回 pending。
# 同一单元被回收 MAX_CLAIM_ATTEMPTS 次后标 blocked，防止毒丸单元被无限重抢。
DEFAULT_LEASE_SECONDS = 7200
MAX_CLAIM_ATTEMPTS = 3

# 盲审（无记忆外部评审）配置：close 之前必须先过一次盲审。
# avg_rating 低于阈值且预算允许时，用评审弱点追加最多 1 轮修订，防止无限循环。
BLIND_REVIEW_RATING_THRESHOLD = 5.5
MIN_BLIND_REVIEWS = 2
MAX_BLIND_REVIEW_ROUNDS = 1
# 盲审产物还没落盘时，把单元退回去等一次再说。SKILL 的契约就是「重试 ≤ 1 次」，
# 引擎此前没有实现它。
MAX_BLIND_REVIEW_ARTIFACT_WAITS = 1
ENGINE_COMMAND = (
    f"{shlex.quote(sys.executable)} "
    f"{shlex.quote(str(Path(__file__).resolve()))}"
)

# 「完成」就是裁决本身的三型单元：分析要拉起 critic 链，critic 要决定下一轮，盲审要按
# 分数裁 close 还是修订。complete 只写状态、不做裁决，官方 run 2 里 result_analysis_c1
# 就是这样被批掉的：critic 与盲审链从未被追加，队列排空却永远差一个 close（#218）。
# This table is the single contract for each adjudication command, artifact, and verdict set.
ADJUDICATION: dict[str, dict[str, Any]] = {
    "result-analysis": {
        "command": "after-result-analysis",
        "after": "完成分析并写入 findings 后",
        "artifact": "state.md",
        "verdicts": {"continue", "stop", "blocked"},
    },
    "critic": {
        "command": "after-critic",
        "after": "召唤 ar-critic 写 critic.md 并把 verdict/next_focus 合并进 state.md 后",
        "artifact": "critic.md",
        "verdicts": {"finish_ok", "approve", "needs_revision", "needs_more_research", "revise", "rerun"},
    },
    "blind-review": {
        "command": "after-blind-review",
        "after": "召唤 ar-blind-reviewer（无记忆盲审）写 blind_review.md 后",
        "artifact": "blind_review.md",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def adjudication_command(project_root: Path, unit: dict[str, Any], worker: str = "") -> str | None:
    """这个单元该用哪条命令收工；不是那三型就返回 None（照旧用 complete）。"""
    spec = ADJUDICATION.get(str(unit.get("type") or ""))
    if spec is None:
        return None
    line = f"{ENGINE_COMMAND} {spec['command']} --project-root {project_root} --unit {unit['id']}"
    return line + (f" --worker {worker}" if worker else "")


def adjudication_freshness_hint(spec: dict[str, Any]) -> str:
    return (
        f"{spec['artifact']} 必须在本次领取后重新写入；领取前已有的文件会被判为 stale。"
    )


def queue_path(project_root: Path) -> Path:
    return project_root / "workflow_queue.json"


def mirror_path(project_root: Path) -> Path:
    return project_root / ENGINE_MIRROR_NAME


def state_path(project_root: Path) -> Path:
    return project_root / "state.md"


def decisions_path(project_root: Path) -> Path:
    return project_root / "decisions.log"


def engine_events_path(project_root: Path) -> Path:
    return project_root / ENGINE_EVENTS_NAME


def workflow_history_markers(project_root: Path) -> list[str]:
    """Return non-empty artifacts that prove this root has already been used."""
    markers: set[str] = set()
    direct_files = (
        engine_events_path(project_root),
        project_root / "plan.md",
        project_root / "review.md",
        project_root / "critic.md",
        project_root / "blind_review.md",
        project_root / "run_manifest.json",
    )
    for path in direct_files:
        if path.is_file() and path.stat().st_size:
            markers.add(str(path.relative_to(project_root)))
    for path in project_root.glob("run_manifest.attempt-*.json"):
        if path.is_file() and path.stat().st_size:
            markers.add(str(path.relative_to(project_root)))
    for directory in (project_root / "code", project_root / "results"):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if (
                path.is_file()
                and path.stat().st_size
                and path.name not in {"notifications.log", "monitor_state.json"}
            ):
                markers.add(str(path.relative_to(project_root)))
    return sorted(markers)


def incomplete_workflow_state(project_root: Path) -> dict[str, Any] | None:
    """Reject init when prior artifacts survive without the engine authorities."""
    queue_exists = queue_path(project_root).is_file()
    mirror_exists = mirror_path(project_root).is_file()
    history = workflow_history_markers(project_root)
    if not history:
        return None
    missing = [
        name
        for name, exists in (
            ("workflow_queue.json", queue_exists),
            (ENGINE_MIRROR_NAME, mirror_exists),
            (ENGINE_EVENTS_NAME, engine_events_path(project_root).is_file()),
        )
        if not exists
    ]
    if queue_exists and mirror_exists and not missing:
        return None
    return {
        "status": "rejected",
        "reason": "incomplete_workflow_state",
        "history": history,
        "missing": missing,
        "hint": "已有运行产物但引擎权威文件不完整；请使用全新的 project root，"
        "不要删除队列或事件账后重新 init。",
    }


def critic_path(project_root: Path) -> Path:
    return project_root / "critic.md"


def blind_review_path(project_root: Path) -> Path:
    return project_root / "blind_review.md"


def adjudication_artifact_path(project_root: Path, unit_type: str) -> Path:
    return project_root / str(ADJUDICATION[unit_type]["artifact"])


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def append_decision(project_root: Path, text: str) -> None:
    path = decisions_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(f"{now_iso()} | step=workflow-engine | {text}\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def engine_event_hash(event: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_engine_events(project_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = engine_events_path(project_root)
    if not path.exists():
        return [], []
    events: list[dict[str, Any]] = []
    problems: list[str] = []
    previous_hash = ""
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            problems.append(f"engine event 第 {line_number} 行不是 JSON：{exc.msg}")
            continue
        if not isinstance(event, dict):
            problems.append(f"engine event 第 {line_number} 行不是对象")
            continue
        if event.get("seq") != line_number:
            problems.append(
                f"engine event 第 {line_number} 行 seq={event.get('seq')}，应为 {line_number}"
            )
        if event.get("previous_hash") != previous_hash:
            problems.append(f"engine event 第 {line_number} 行 previous_hash 断链")
        expected_hash = engine_event_hash(event)
        if event.get("event_hash") != expected_hash:
            problems.append(f"engine event 第 {line_number} 行 hash 不匹配")
        previous_hash = str(event.get("event_hash") or "")
        events.append(event)
    return events, problems


def append_engine_event(project_root: Path, kind: str, **fields: Any) -> dict[str, Any]:
    events, problems = read_engine_events(project_root)
    if problems:
        raise RuntimeError("; ".join(problems))
    event = {
        "schema_version": COMPLETION_AUTHORITY_VERSION,
        "seq": len(events) + 1,
        "previous_hash": str(events[-1].get("event_hash") or "") if events else "",
        "kind": kind,
        "at": now_iso(),
        **fields,
    }
    event["event_hash"] = engine_event_hash(event)
    path = engine_events_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


def record_terminal_unit(
    project_root: Path,
    unit: dict[str, Any],
    source: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    status = str(unit.get("status") or "")
    if status not in TERMINAL_STATUSES:
        return
    append_engine_event(
        project_root,
        "unit_terminal",
        unit=str(unit.get("id") or ""),
        unit_type=str(unit.get("type") or ""),
        cycle=int(unit.get("cycle", 0) or 0),
        status=status,
        ended_at=unit.get("ended_at"),
        reason=unit.get("reason"),
        source=source,
        evidence=evidence or {},
    )


def initial_units() -> list[dict[str, Any]]:
    return [
        {"id": "spawn_agents", "cycle": 0, "type": "agent", "status": "pending"},
        {"id": "plan_gate", "cycle": 0, "type": "planning", "status": "pending", "blocked_by": "spawn_agents"},
        {"id": "code_gate", "cycle": 0, "type": "coding", "status": "pending", "blocked_by": "plan_gate"},
        {"id": "code_review", "cycle": 0, "type": "review", "status": "pending", "blocked_by": "code_gate"},
        {
            "id": "run_pilot_experiment",
            "cycle": 0,
            "type": "run",
            "stage": "pilot",
            "status": "pending",
            "blocked_by": "code_review",
        },
        {
            "id": "pilot_result_analysis",
            "cycle": 0,
            "type": "result-analysis",
            "stage": "pilot",
            "status": "pending",
            "blocked_by": "run_pilot_experiment",
        },
        {
            "id": "planner_scale_up",
            "cycle": 0,
            "type": "planning",
            "stage": "main",
            "status": "pending",
            "blocked_by": "pilot_result_analysis",
        },
        {
            "id": "code_main_experiment",
            "cycle": 0,
            "type": "coding",
            "stage": "main",
            "status": "pending",
            "blocked_by": "planner_scale_up",
        },
        {
            "id": "review_main_experiment",
            "cycle": 0,
            "type": "review",
            "stage": "main",
            "status": "pending",
            "blocked_by": "code_main_experiment",
        },
        {
            "id": "run_main_experiment",
            "cycle": 0,
            "type": "run",
            "stage": "main",
            "status": "pending",
            "blocked_by": "review_main_experiment",
        },
        {
            "id": "main_result_analysis",
            "cycle": 0,
            "type": "result-analysis",
            "stage": "main",
            "status": "pending",
            "blocked_by": "run_main_experiment",
        },
    ]


def normalize_queue(queue: dict[str, Any]) -> dict[str, Any]:
    units = queue.get("units")
    if not isinstance(units, list) or not units:
        units = initial_units()

    seen: set[str] = set()
    normalized_units: list[dict[str, Any]] = []
    for idx, raw in enumerate(units):
        if not isinstance(raw, dict):
            raise ValueError(f"unit at index {idx} must be an object")
        unit = dict(raw)
        unit_id = str(unit.get("id") or "").strip()
        if not unit_id:
            raise ValueError(f"unit at index {idx} has no id")
        if unit_id in seen:
            raise ValueError(f"duplicate unit id: {unit_id}")
        seen.add(unit_id)
        status = str(unit.get("status", "pending"))
        if status not in VALID_STATUSES:
            status = "pending"
        unit["id"] = unit_id
        unit["status"] = status
        unit["cycle"] = int(unit.get("cycle", 0) or 0)
        normalized_units.append(unit)

    queue = dict(queue)
    queue["mode"] = "autoresearch_loop"
    queue["schema_version"] = 1
    queue["max_cycles"] = int(queue.get("max_cycles", DEFAULT_MAX_CYCLES) or DEFAULT_MAX_CYCLES)
    queue["current_cycle"] = int(queue.get("current_cycle", 0) or 0)
    queue["iteration"] = max(
        int(queue.get("iteration", 0) or 0),
        sum(1 for unit in normalized_units if unit.get("status") in TERMINAL_STATUSES),
    )
    queue["units"] = normalized_units

    cycle_status = queue.get("cycle_status")
    if not isinstance(cycle_status, dict):
        cycle_status = {}
    for unit in normalized_units:
        key = str(unit.get("cycle", 0))
        cycle_status.setdefault(
            key,
            {
                "stage": "initial" if key == "0" else "iteration",
                "status": "running" if key == str(queue["current_cycle"]) else "pending",
                "started_at": now_iso(),
                "ended_at": None,
                "key_findings": [],
                "next_focus": [],
                "stop_reason": None,
            },
        )
    queue["cycle_status"] = cycle_status
    return queue


def write_queue(project_root: Path, queue: dict[str, Any]) -> None:
    """引擎写队列的唯一出口：自增 engine_seq，同时留一份完整镜像。

    镜像是引擎最后写出去的那份的拷贝，所以它既是对账基准，也是恢复用的原件——报错能
    直接告诉人 `cp` 哪一份回去。
    """
    queue["engine_seq"] = int(queue.get("engine_seq", 0) or 0) + 1
    write_json_atomic(queue_path(project_root), queue)
    write_json_atomic(mirror_path(project_root), queue)


def protected_cycle_skip_provenance(queue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cycle_status = queue.get("cycle_status")
    if not isinstance(cycle_status, dict):
        return {}
    protected: dict[str, dict[str, Any]] = {}
    for cycle, raw in cycle_status.items():
        if not isinstance(raw, dict):
            continue
        has_skip_record = any(
            field in raw for field in ("analysis_decision", "critic_decision", "skip_record")
        )
        if raw.get("status") != "skipped" and not has_skip_record:
            continue
        protected[str(cycle)] = {
            "status": raw.get("status"),
            "ended_at": raw.get("ended_at"),
            "analysis_decision": raw.get("analysis_decision"),
            "critic_decision": raw.get("critic_decision"),
            "skip_record": raw.get("skip_record"),
        }
    return protected


def protected_skipped_units(queue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    protected: dict[str, dict[str, Any]] = {}
    for unit in queue.get("units", []):
        reason = str(unit.get("reason") or "")
        if unit.get("status") != "skipped" and not reason.startswith("cycle_mooted"):
            continue
        protected[str(unit.get("id"))] = {
            "status": unit.get("status"),
            "reason": reason,
            "ended_at": unit.get("ended_at"),
        }
    return protected


def protected_adjudication_units(queue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return decision-bearing units whose state may only move through the engine."""
    return {
        str(unit.get("id")): unit
        for unit in queue.get("units", [])
        if unit.get("type") in ADJUDICATION
    }


def queue_regression(project_root: Path, queue: dict[str, Any]) -> dict[str, Any] | None:
    """这份队列还是引擎写出去的那份吗（#218）。

    fork run 1 把 21 个单元（10 done / 5 skipped）的队列一次性换成 11 个单元的模板：
    verify-close 挡住了假完成，但挡不住进度回退，后面的 attempt 全在重跑做完的事。
    两个判据各挡一种形状：seq 倒退挡整体重写，单元丢失挡「连 seq 一起抄过去」的重写。

    Unit state and topology are engine-owned. Accepting a live-only status edit before
    the next write would launder it into the mirror and can unlock downstream work
    without the terminal evidence required by complete.
    """
    mirror = read_json(mirror_path(project_root))
    if not mirror:
        # 引擎从没写过这个项目，没有可对账的过去。第一次写会把镜像建起来。
        return None
    live_seq = int(queue.get("engine_seq", 0) or 0)
    mirror_seq = int(mirror.get("engine_seq", 0) or 0)
    live_ids = {str(unit.get("id")) for unit in queue.get("units", [])}
    lost = [str(unit.get("id")) for unit in mirror.get("units", [])
            if str(unit.get("id")) not in live_ids]
    protected_fields = []
    if queue.get("completion_authority_version") != mirror.get("completion_authority_version"):
        protected_fields.append("completion_authority_version")
    if queue.get("last_analysis_decision") != mirror.get("last_analysis_decision"):
        protected_fields.append("last_analysis_decision")
    if queue.get("cycle_skip_records") != mirror.get("cycle_skip_records"):
        protected_fields.append("cycle_skip_records")
    if protected_cycle_skip_provenance(queue) != protected_cycle_skip_provenance(mirror):
        protected_fields.append("cycle_status.skip_provenance")
    if protected_skipped_units(queue) != protected_skipped_units(mirror):
        protected_fields.append("units.skipped_status_reason")
    if protected_adjudication_units(queue) != protected_adjudication_units(mirror):
        protected_fields.append("units.adjudication_state")
    if queue.get("units") != mirror.get("units"):
        protected_fields.append("units.engine_state")
    if live_seq >= mirror_seq and not lost and not protected_fields:
        return None
    return {
        "status": "rejected",
        "reason": "queue_rewritten",
        "engine_seq": live_seq,
        "engine_seq_expected": mirror_seq,
        "lost_units": lost,
        "protected_fields": protected_fields,
        "hint": f"workflow_queue.json 不是引擎最后写出去的那份。引擎写的那份在 "
                f"{ENGINE_MIRROR_NAME}：核对之后 `cp {ENGINE_MIRROR_NAME} workflow_queue.json` "
                f"就能接着跑；确实要从头开始，把这两份一起删掉再 init。",
    }


def load_queue(project_root: Path) -> dict[str, Any]:
    """引擎读队列的唯一入口：读、规范化、对账。

    对账放在读这一侧，是因为写、判定和发任务都从这里取队列——挡在读上，被重写的队列
    既不会被当成事实发下去，也不会被下一次写固化成新的基准。
    """
    queue = normalize_queue(read_json(queue_path(project_root)))
    problem = queue_regression(project_root, queue)
    if problem is not None:
        print(json.dumps(problem, ensure_ascii=False))
        raise SystemExit(7)
    return queue


def unit_by_id(queue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {unit["id"]: unit for unit in queue.get("units", [])}


def blocked_by_done(queue: dict[str, Any], unit: dict[str, Any]) -> bool:
    blocker = unit.get("blocked_by")
    if not blocker:
        return True
    units = unit_by_id(queue)
    if isinstance(blocker, list):
        return all(
            units.get(str(item), {}).get("status") in SATISFIED_DEPENDENCY_STATUSES
            for item in blocker
        )
    return units.get(str(blocker), {}).get("status") in SATISFIED_DEPENDENCY_STATUSES


def failed_unit_gaps(queue: dict[str, Any]) -> list[str]:
    return [
        f"required unit {unit.get('id')} 处于 failed"
        for unit in queue.get("units", [])
        if unit.get("type") != "close" and unit.get("status") == "failed"
    ]


def next_unit(queue: dict[str, Any]) -> dict[str, Any] | None:
    for unit in queue.get("units", []):
        if unit.get("status") in ACTIVE_STATUSES and blocked_by_done(queue, unit):
            return unit
    return None


def lock_path(project_root: Path) -> Path:
    return project_root / "workflow_queue.lock"


@contextmanager
def queue_lock(project_root: Path):
    """flock 串行化所有 workflow_queue.json 的读改写，是抢占原子性的唯一来源。"""
    path = lock_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def lease_active(unit: dict[str, Any]) -> bool:
    expires = parse_iso(unit.get("lease_expires_at"))
    return expires is not None and expires > datetime.now(timezone.utc)


def reclaim_expired_leases(queue: dict[str, Any], project_root: Path) -> list[str]:
    """回收失联 worker 的单元：租约过期 → 回 pending；被回收满 MAX_CLAIM_ATTEMPTS 次 → blocked。"""
    reclaimed: list[str] = []
    for unit in queue.get("units", []):
        if unit.get("status") != "running" or not unit.get("claimed_by"):
            continue
        if lease_active(unit):
            continue
        attempts = int(unit.get("claim_attempts", 0) or 0)
        lost_worker = unit.get("claimed_by")
        unit.pop("claimed_by", None)
        unit.pop("lease_expires_at", None)
        if attempts >= MAX_CLAIM_ATTEMPTS:
            unit["status"] = "blocked"
            unit["blocked_reason"] = f"reclaimed_{attempts}_times_last_worker={lost_worker}"
            append_decision(
                project_root,
                f"event=unit_poisoned unit={unit['id']} attempts={attempts} last_worker={lost_worker}",
            )
        else:
            unit["status"] = "pending"
            append_decision(
                project_root,
                f"event=lease_reclaimed unit={unit['id']} lost_worker={lost_worker} attempts={attempts}",
            )
        reclaimed.append(unit["id"])
    return reclaimed


def claimable_units(queue: dict[str, Any], types: set[str] | None = None) -> list[dict[str, Any]]:
    """当前可被抢占的单元：pending / running-but-incomplete、依赖已满足、无有效租约。

    注意：串行模式(next-prompt)标成 running 的单元没有租约字段，这里不视为可抢，
    避免 worker 把 coordinator 正在做的单元偷走。
    """
    ready: list[dict[str, Any]] = []
    for unit in queue.get("units", []):
        if unit.get("status") not in {"pending", "running-but-incomplete"}:
            continue
        if not blocked_by_done(queue, unit):
            continue
        if types and unit.get("type") not in types:
            continue
        ready.append(unit)
    return ready


def adjudication_hints(project_root: Path, unit_id: str) -> list[str]:
    """next-prompt 给协调器的「这型单元怎么收工」。它拿到的是哪型单元由它自己判断。"""
    lines: list[str] = []
    for unit_type, spec in ADJUDICATION.items():
        lines.append(f"如果这是 {unit_type} 单元，{spec['after']}，运行：")
        lines.append(adjudication_freshness_hint(spec))
        lines.append(f"{ENGINE_COMMAND} {spec['command']} --project-root {project_root} --unit {unit_id}")
    return lines


def worker_unit_prompt(
    project_root: Path,
    unit: dict[str, Any],
    worker: str,
    counts: dict[str, int],
    ready_remaining: int,
) -> str:
    engine = ENGINE_COMMAND
    lines = [
        f"你是 AutoResearch worker `{worker}`，刚抢占到一个工作单元。",
        f"project_root = {project_root}",
        f"unit = {unit['id']}",
        f"cycle = {unit.get('cycle', 0)}",
        f"type = {unit.get('type', 'unknown')}",
        f"stage = {unit.get('stage', 'none')}",
        f"lease_expires_at = {unit.get('lease_expires_at')}",
        f"queue_counts = {counts}",
        f"ready_remaining = {ready_remaining}",
        "",
        "只执行这个 unit。执行前读取 state.md、workflow_queue.json、decisions.log 最近事件。",
        "预计超过租约时长时先续租："
        f"{engine} heartbeat --project-root {project_root} --unit {unit['id']} --worker {worker}",
    ]
    adjudication = adjudication_command(project_root, unit, worker)
    if adjudication:
        # 提示和门必须读同一处：这里还写着 complete 的话，worker 会照做，然后撞上 exit 6。
        spec = ADJUDICATION[unit["type"]]
        lines.append(f"这型单元的完成就是裁决本身，complete 会被拒。{spec['after']}运行：")
        lines.append(adjudication_freshness_hint(spec))
        lines.append(adjudication)
    else:
        if unit.get("type") == "run":
            lines.append(
                f"只通过 execute-run 执行 stage={expected_run_stage(unit)}；命令必须使用 "
                "<project_root>/.venv/bin/python，并显式绑定当前 unit 的 --artifact-dir 与共享 "
                "--run-log。run 完成前写 results/run_receipts/<unit>.json，引用 execute-run "
                "返回的 execution_event_hash，并逐项列出当前 unit 目录里的全部普通文件。"
            )
        elif unit.get("type") == "review":
            lines.append(
                "必须调用 ar-gemini-review MCP 持久化本轮 review.md；engine 会核对 MCP producer "
                "receipt、unit/cycle、blockers_count 和真实 model identity。"
            )
        lines.append(
            f"完成后回写（必须带 --worker，租约被回收会拒绝）：{engine} complete "
            f"--project-root {project_root} --unit {unit['id']} --worker {worker} "
            f"--status done --result \"<一句话>\""
        )
    lines += [
        f"无法完成时归还：{engine} release --project-root {project_root} --unit {unit['id']} --worker {worker}",
        f"回写成功后继续抢下一个：{engine} claim --project-root {project_root} --worker {worker} --prompt",
        "claim 返回 status=empty 且 pending=0 时结束循环；不要输出 AUTORESEARCH_DONE（收尾是 coordinator 的事）。",
    ]
    return "\n".join(lines)


def queue_counts(queue: dict[str, Any]) -> dict[str, int]:
    counts = {"pending": 0, "running": 0, "done": 0, "failed": 0, "skipped": 0, "blocked": 0}
    for unit in queue.get("units", []):
        status = unit.get("status", "pending")
        if status == "running-but-incomplete":
            status = "running"
        counts[status] = counts.get(status, 0) + 1
    return counts


BULLET_LINE = re.compile(r"^[ \t]*-[ \t]*(.+?)[ \t]*:[ \t]*(.*)$", re.MULTILINE)


def normalize_field(name: str) -> str:
    """字段名归一：去掉两端的强调符，下划线连字符空格等价，不分大小写。

    只作用于字段名。值不参与归一——`phase_2_complete` 里的下划线是内容。
    """
    return re.sub(r"[\s_-]+", " ", name.strip(" *_`")).strip().lower()


def parse_bullet_field(text: str, field: str, aliases: tuple[str, ...] = ()) -> list[str]:
    """读第一行 `- <字段>: <值>`。

    字段名按 normalize_field 匹配，所以 `- **Average Rating**: 7.8` 和
    `- avg_rating: 7.8` 一样读得到。模型给字段名加粗或换个同义词是常态，而合同没
    对上时整份产物会被当成不存在：#241 那次一份真实的 7.8/10 ACCEPT 就是这样在账本
    上变成「盲审做不成」的。值只剥两端的强调符，内部一个字符都不动。
    """
    wanted = {normalize_field(field), *(normalize_field(alias) for alias in aliases)}
    for match in BULLET_LINE.finditer(text):
        if normalize_field(match.group(1)) not in wanted:
            continue
        raw = match.group(2).strip().strip("*`").strip()
        if not raw or raw.lower() in {"none", "null", "n/a"}:
            return []
        parts = [part.strip(" ;,") for part in re.split(r"[;；]|,\s+|，", raw) if part.strip(" ;,")]
        return parts[:5]
    return []


def declared_bullet_fields(text: str) -> set[str]:
    return {normalize_field(match.group(1)) for match in BULLET_LINE.finditer(text)}


def read_cycle_report(project_root: Path) -> dict[str, Any]:
    path = state_path(project_root)
    text = path.read_text(errors="replace") if path.exists() else ""
    next_focus = parse_bullet_field(text, "next_focus")
    key_findings = parse_bullet_field(text, "key_findings")
    stop_reason_values = parse_bullet_field(text, "stop_reason")
    phase_2_skip = parse_bullet_field(text, "phase_2_skipped_reason")
    stop_reason = stop_reason_values[0] if stop_reason_values else ""
    if stop_reason.lower() in {"none", "null", "n/a"}:
        stop_reason = ""
    return {
        "present": path.exists(),
        "structured": {
            "key findings",
            "next focus",
            "stop reason",
        }.issubset(declared_bullet_fields(text)),
        "key_findings": key_findings,
        "next_focus": next_focus,
        "stop_reason": stop_reason,
        "phase_2_skipped_reason": phase_2_skip[0] if phase_2_skip else "",
    }


def read_critic_report(project_root: Path) -> dict[str, Any]:
    path = critic_path(project_root)
    text = path.read_text(errors="replace") if path.exists() else ""
    verdict_values = parse_bullet_field(text, "verdict")
    focus = parse_bullet_field(text, "required_next_focus")
    optional_focus = parse_bullet_field(text, "optional_next_focus")
    stop_reason_values = parse_bullet_field(text, "stop_reason")
    unit_values = parse_bullet_field(text, "unit")
    cycle_values = parse_bullet_field(text, "cycle")
    verdict = verdict_values[0] if verdict_values else ""
    stop_reason = stop_reason_values[0] if stop_reason_values else ""
    try:
        cycle = int(cycle_values[0]) if cycle_values else None
    except ValueError:
        cycle = None
    return {
        "unit": unit_values[0] if unit_values else "",
        "cycle": cycle,
        "verdict": verdict,
        "required_next_focus": focus,
        "optional_next_focus": optional_focus,
        "stop_reason": stop_reason,
    }


def critic_consensus_verdict(verdicts: list[str]) -> str:
    if "needs_revision" in verdicts:
        return "needs_revision"
    if "needs_more_research" in verdicts:
        return "needs_more_research"
    if verdicts and set(verdicts) == {"finish_ok"}:
        return "finish_ok"
    return ""


def critic_receipt_payload_gaps(
    payload: Any,
    unit: dict[str, Any],
    artifact: Path | None = None,
    report: dict[str, Any] | None = None,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["critic receipt 不是对象"]
    gaps: list[str] = []
    unit_id = str(unit.get("id") or "")
    cycle = int(unit.get("cycle", 0) or 0)
    if payload.get("schema_version") != CRITIC_RECEIPT_SCHEMA_VERSION:
        gaps.append("critic receipt schema_version 无效")
    if payload.get("unit") != unit_id:
        gaps.append("critic receipt unit 与目标单元不一致")
    if payload.get("cycle") != cycle:
        gaps.append("critic receipt cycle 与目标单元不一致")
    if payload.get("artifact") != "critic.md":
        gaps.append("critic receipt artifact 不是 critic.md")
    receipt_sha = str(payload.get("artifact_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", receipt_sha) is None:
        gaps.append("critic receipt artifact_sha256 无效")
    if artifact is not None:
        artifact_sha = sha256_file(artifact) if artifact.is_file() else ""
        if receipt_sha != artifact_sha:
            gaps.append("critic receipt artifact SHA256 与当前 critic.md 不一致")
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"finish_ok", "needs_revision", "needs_more_research"}:
        gaps.append("critic receipt verdict 无效")
    if report is not None:
        report_verdict = str(report.get("verdict") or "").strip().lower()
        if verdict != report_verdict:
            gaps.append("critic receipt verdict 与 critic.md 不一致")
    try:
        uuid.UUID(str(payload.get("request_id") or ""))
    except ValueError:
        gaps.append("critic receipt request_id 不是 UUID")
    issued_at = parse_iso(payload.get("issued_at"))
    started_at = parse_iso(unit.get("started_at"))
    if issued_at is None:
        gaps.append("critic receipt issued_at 无效")
    elif started_at is None or issued_at < started_at:
        gaps.append("critic receipt issued_at 早于本次 critic 单元启动")
    elif issued_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        gaps.append("critic receipt issued_at 晚于当前时间")

    critics = payload.get("critics")
    if not isinstance(critics, list):
        return [*gaps, "critic receipt critics 不是数组"]
    by_role: dict[str, dict[str, Any]] = {}
    for item in critics:
        if not isinstance(item, dict):
            gaps.append("critic receipt critics 含非对象条目")
            continue
        role = str(item.get("role") or "")
        if role in by_role:
            gaps.append(f"critic receipt role 重复：{role}")
        by_role[role] = item
    if set(by_role) != {"critic", "critic_secondary"}:
        gaps.append("critic receipt 必须记录 critic 和 critic_secondary 两个角色")

    identities: list[str] = []
    parsed_verdicts: list[str] = []
    for role in ("critic", "critic_secondary"):
        item = by_role.get(role)
        if item is None:
            continue
        status = item.get("status")
        if role == "critic" and status != "ok":
            gaps.append("critic receipt 主 critic 未成功")
            continue
        if status != "ok":
            gaps.append(f"critic receipt {role} 状态不是 ok")
            continue
        model = str(item.get("model") or "").strip()
        identity = str(item.get("model_identity") or "").strip().lower()
        response_sha = str(item.get("response_sha256") or "")
        item_verdict = str(item.get("verdict") or "").strip().lower()
        if not model:
            gaps.append(f"critic receipt {role} 缺少 model")
        if not identity:
            gaps.append(f"critic receipt {role} 缺少 model_identity")
        elif identity in identities:
            gaps.append("critic receipt 两个成功角色使用了相同 model_identity")
        else:
            identities.append(identity)
        if re.fullmatch(r"[0-9a-f]{64}", response_sha) is None:
            gaps.append(f"critic receipt {role} response_sha256 无效")
        if item_verdict not in {"finish_ok", "needs_revision", "needs_more_research"}:
            gaps.append(f"critic receipt {role} verdict 无效")
        else:
            parsed_verdicts.append(item_verdict)
    if critic_consensus_verdict(parsed_verdicts) != verdict:
        gaps.append("critic receipt 各角色裁决不能推出 critic.md 的共识 verdict")
    return gaps


def critic_receipt_evidence(
    project_root: Path,
    unit: dict[str, Any],
    artifact: Path,
    report: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    events, problems = read_engine_events(project_root)
    candidates = [
        event
        for event in events
        if event.get("kind") == "critic_receipt"
        and event.get("unit") == unit.get("id")
        and event.get("cycle") == int(unit.get("cycle", 0) or 0)
    ]
    if not candidates:
        return [*problems, "当前 critic 单元没有 MCP producer receipt"], {}
    event = candidates[-1]
    gaps = list(problems)
    if event.get("source") != CRITIC_RECEIPT_SOURCE:
        gaps.append("critic receipt event source 无效")
    event_at = parse_iso(event.get("at"))
    started_at = parse_iso(unit.get("started_at"))
    if not event_at or not started_at or event_at < started_at:
        gaps.append("critic receipt event 早于本次 critic 单元启动")
    payload = event.get("receipt")
    gaps.extend(critic_receipt_payload_gaps(payload, unit, artifact, report))
    receipt = payload if isinstance(payload, dict) else {}
    critics = receipt.get("critics") if isinstance(receipt.get("critics"), list) else []
    return gaps, {
        "event_hash": event.get("event_hash"),
        "request_id": receipt.get("request_id"),
        "artifact_sha256": receipt.get("artifact_sha256"),
        "verdict": receipt.get("verdict"),
        "model_identities": [
            item.get("model_identity")
            for item in critics
            if isinstance(item, dict) and item.get("status") == "ok"
        ],
        "response_sha256": [
            item.get("response_sha256")
            for item in critics
            if isinstance(item, dict) and item.get("status") == "ok"
        ],
    }


def artifact_belongs_to(path: Path, unit: dict[str, Any]) -> bool:
    """全局单文件产物是不是这一轮写的。

    产物是全局单文件，而修订之后会再排一个同型单元。只检查「文件在」的话，第二轮可以
    拿第一轮的产物过门——#127 封住的「手工标 done」换个凭据又开了：从「没有报告」变成
    「上一轮的报告」。critic 侧同样的洞是 #252：Phase 2 的 finish_ok 写在别的文件名下，
    引擎读到 Phase 1 的 needs_revision，凭它追加了一条谁都不需要的修订链。

    判据是文件的修改时间晚于这个单元开始的时间。不改产物格式、不要求 agent 配合，
    因为格式本来就不稳（散文体那次 n_reviews 解析成 0）。缺少 started_at 就无法证明
    freshness，必须退回 pending，让单元通过 claim 重新起跑。
    """
    started = unit.get("started_at")
    if not started:
        return False
    if not path.exists():
        return False
    try:
        written = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        began = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    except (OSError, ValueError):
        return False
    return written >= began


def project_relative_path(project_root: Path, raw: Any) -> tuple[Path | None, str]:
    value = str(raw or "").strip()
    if not value:
        return None, "路径为空"
    candidate = (project_root / value).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError:
        return None, f"路径逃逸 project root：{value}"
    return candidate, ""


def run_receipt_path(project_root: Path, unit: dict[str, Any]) -> Path:
    return project_root / "results" / "run_receipts" / f"{unit['id']}.json"


def lexical_project_path(
    project_root: Path,
    raw: Any,
    *,
    base: Path | None = None,
) -> tuple[Path | None, str]:
    value = str(raw or "").strip()
    if not value:
        return None, "路径为空"
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = (base or project_root) / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        candidate.relative_to(project_root)
    except ValueError:
        return None, f"路径逃逸 project root：{value}"
    return candidate, ""


def expected_run_stage(unit: dict[str, Any]) -> str:
    declared = str(unit.get("stage") or "").strip()
    if declared:
        return declared
    unit_id = str(unit.get("id") or "")
    if "pilot" in unit_id:
        return "pilot"
    if "main" in unit_id:
        return "main"
    return "iteration"


def argv_option_values(argv: list[str], option: str) -> list[str]:
    values = []
    for index, value in enumerate(argv):
        if value == option:
            values.append(argv[index + 1] if index + 1 < len(argv) else "")
        prefix = f"{option}="
        if value.startswith(prefix):
            values.append(value[len(prefix):])
    return values


def single_argv_option(argv: list[str], option: str, gaps: list[str]) -> str:
    values = argv_option_values(argv, option)
    if len(values) != 1:
        gaps.append(f"run argv {option} 必须恰好出现一次")
        return ""
    return values[0]


def run_execution_evidence(
    project_root: Path,
    unit: dict[str, Any],
    receipt: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    gaps: list[str] = []
    events, event_problems = read_engine_events(project_root)
    gaps.extend(event_problems)
    wanted_hash = str(receipt.get("execution_event_hash") or "")
    event = next(
        (
            item
            for item in events
            if item.get("kind") == "run_execution"
            and item.get("event_hash") == wanted_hash
        ),
        None,
    )
    if event is None:
        return [*gaps, "run execution event 不存在或 hash 不匹配"], {}

    expected_stage = expected_run_stage(unit)
    matching_executions = [
        item
        for item in events
        if item.get("kind") == "run_execution"
        and item.get("unit") == unit.get("id")
        and item.get("cycle") == int(unit.get("cycle", 0) or 0)
    ]
    if matching_executions and event is not matching_executions[-1]:
        gaps.append("run receipt 没有引用本 unit/cycle 最新一次 run execution")
    if event.get("source") != "execute-run":
        gaps.append("run execution event source 不是 execute-run")
    if event.get("unit") != unit.get("id"):
        gaps.append("run execution event unit 与目标单元不一致")
    if event.get("cycle") != int(unit.get("cycle", 0) or 0):
        gaps.append("run execution event cycle 与目标单元不一致")
    if event.get("stage") != expected_stage:
        gaps.append(
            f"run execution stage={event.get('stage')}，当前 unit 要求 {expected_stage}"
        )
    if event.get("exit_code") != 0:
        gaps.append("run execution 没有 exit_code=0")
    if event.get("contract_gaps") not in ([], None):
        gaps.append("run execution 记录了合同缺口")
    event_started = parse_iso(event.get("started_at"))
    event_finished = parse_iso(event.get("finished_at"))
    unit_started = parse_iso(unit.get("started_at"))
    if not event_started or not event_finished or event_finished < event_started:
        gaps.append("run execution 起止时间无效")
    elif unit_started and event_started < unit_started:
        gaps.append("run execution 早于本次 unit 启动")

    cwd, cwd_error = lexical_project_path(project_root, event.get("cwd"))
    if cwd_error or cwd is None:
        gaps.append(f"run execution cwd 无效：{cwd_error}")
        cwd = project_root

    environment = event.get("environment")
    environment = environment if isinstance(environment, dict) else {}
    if environment.get("kind") != "venv":
        gaps.append("run execution 未使用项目 venv")
    prefix, prefix_error = lexical_project_path(project_root, environment.get("prefix"))
    expected_prefix = project_root / ".venv"
    if prefix_error or prefix != expected_prefix:
        gaps.append("run execution venv prefix 必须是 <project_root>/.venv")
    elif not (prefix / "pyvenv.cfg").is_file():
        gaps.append("run execution venv 缺少 pyvenv.cfg")
    python, python_error = lexical_project_path(project_root, environment.get("python"))
    if python_error or python is None or prefix is None:
        gaps.append("run execution Python 路径无效")
    else:
        try:
            python.relative_to(prefix)
        except ValueError:
            gaps.append("run execution Python 不在项目 venv 内")
        if not python.is_file():
            gaps.append("run execution Python 不存在")
        elif environment.get("python_sha256") != sha256_file(python):
            gaps.append("run execution Python SHA256 不匹配")

    argv_raw = event.get("argv")
    argv = argv_raw if isinstance(argv_raw, list) and all(
        isinstance(item, str) and item for item in argv_raw
    ) else []
    if not argv:
        gaps.append("run execution argv 无效")
    else:
        command, command_error = lexical_project_path(project_root, argv[0], base=cwd)
        if command_error or python is None or command != python:
            gaps.append("run execution argv[0] 不是已核验的项目 venv Python")
        stage_arg = single_argv_option(argv, "--stage", gaps)
        artifact_value = single_argv_option(argv, "--artifact-dir", gaps)
        run_log_value = single_argv_option(argv, "--run-log", gaps)
        if stage_arg != expected_stage:
            gaps.append(f"run execution argv 未绑定 --stage {expected_stage}")
        artifact_arg, artifact_error = lexical_project_path(
            project_root,
            artifact_value,
            base=cwd,
        )
        expected_artifact = (
            project_root / "results" / "run_artifacts" / str(unit.get("id"))
        )
        if artifact_error or artifact_arg != expected_artifact:
            gaps.append(
                "run execution argv 未绑定当前 unit 的 --artifact-dir"
            )
        run_log_arg, run_log_error = lexical_project_path(
            project_root,
            run_log_value,
            base=cwd,
        )
        if run_log_error or run_log_arg != project_root / "results" / "run.log":
            gaps.append("run execution argv 未绑定共享 --run-log")

    artifact_dir, artifact_error = lexical_project_path(
        project_root,
        event.get("artifact_dir"),
    )
    expected_artifact = project_root / "results" / "run_artifacts" / str(unit.get("id"))
    if artifact_error or artifact_dir != expected_artifact:
        gaps.append("run execution event artifact_dir 与当前 unit 不一致")

    run_log = event.get("run_log")
    run_log = run_log if isinstance(run_log, dict) else {}
    run_log_path, run_log_error = lexical_project_path(project_root, run_log.get("path"))
    if run_log_error or run_log_path != project_root / "results" / "run.log":
        gaps.append("run execution event run_log 路径无效")
    if not isinstance(run_log.get("before_bytes"), int) or not isinstance(
        run_log.get("after_bytes"), int
    ):
        gaps.append("run execution event 未记录 run_log 字节边界")
    elif run_log["after_bytes"] < run_log["before_bytes"]:
        gaps.append("run execution 截断了共享 run_log")

    attempt = event.get("attempt")
    attempt = attempt if isinstance(attempt, dict) else {}
    attempt_path, attempt_error = lexical_project_path(
        project_root,
        attempt.get("path"),
    )
    if attempt_error or attempt_path is None:
        gaps.append("run execution attempt 路径无效")
    else:
        try:
            attempt_path.relative_to(expected_artifact)
        except ValueError:
            gaps.append("run execution attempt 不在当前 unit artifact 目录")
        if not attempt_path.is_file():
            gaps.append("run execution attempt 不存在")
        elif attempt.get("sha256") != sha256_file(attempt_path):
            gaps.append("run execution attempt SHA256 不匹配")

    return gaps, {
        "event_hash": event.get("event_hash"),
        "stage": event.get("stage"),
        "argv": argv,
        "environment": environment,
        "attempt": attempt,
        "run_log": run_log,
    }


def run_artifact_namespace_gaps(
    project_root: Path,
    queue: dict[str, Any],
    current_unit: dict[str, Any],
) -> list[str]:
    root = project_root / "results" / "run_artifacts"
    if not root.exists():
        return []
    allowed = {str(current_unit.get("id") or "")}
    allowed.update(
        str(unit.get("id") or "")
        for unit in queue.get("units", [])
        if unit.get("type") == "run" and unit.get("status") in {"running", "done"}
    )
    gaps = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.name in allowed:
            continue
        gaps.append(
            f"旁路 run artifact 不属于已封存或当前 unit："
            f"{path.relative_to(project_root)}"
        )
    return gaps


def run_command_contract(
    project_root: Path,
    unit: dict[str, Any],
    argv: list[str],
) -> tuple[list[str], dict[str, Any]]:
    gaps: list[str] = []
    expected_stage = expected_run_stage(unit)
    python = project_root / ".venv" / "bin" / "python"
    prefix = project_root / ".venv"
    artifact_dir = project_root / "results" / "run_artifacts" / str(unit.get("id"))
    run_log = project_root / "results" / "run.log"
    if not (prefix / "pyvenv.cfg").is_file() or not python.is_file():
        gaps.append("项目 venv 不可用：先创建 <project_root>/.venv")
    if not argv:
        gaps.append("run argv 为空")
    else:
        command, command_error = lexical_project_path(project_root, argv[0])
        if command_error or command != python:
            gaps.append("run argv[0] 必须是 <project_root>/.venv/bin/python")
        stage_arg = single_argv_option(argv, "--stage", gaps)
        artifact_value = single_argv_option(argv, "--artifact-dir", gaps)
        run_log_value = single_argv_option(argv, "--run-log", gaps)
        if stage_arg != expected_stage:
            gaps.append(f"run argv 未绑定 --stage {expected_stage}")
        artifact_arg, artifact_error = lexical_project_path(
            project_root,
            artifact_value,
        )
        if artifact_error or artifact_arg != artifact_dir:
            gaps.append("run argv 未绑定当前 unit 的 --artifact-dir")
        run_log_arg, run_log_error = lexical_project_path(
            project_root,
            run_log_value,
        )
        if run_log_error or run_log_arg != run_log:
            gaps.append("run argv 未绑定共享 --run-log")
    environment = {
        "kind": "venv",
        "prefix": ".venv",
        "python": ".venv/bin/python",
        "python_sha256": sha256_file(python) if python.is_file() else "",
    }
    return gaps, environment


def seal_artifact_tree(path: Path) -> None:
    if path.is_symlink():
        return
    if path.is_dir():
        for child in path.iterdir():
            seal_artifact_tree(child)
        path.chmod(0o555)
    elif path.exists():
        path.chmod(0o444)


def prepare_run_artifact_namespaces(
    project_root: Path,
    queue: dict[str, Any],
) -> None:
    """Prepare every claimed run directory while the caller holds the queue lock."""
    artifact_root = project_root / "results" / "run_artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_root.chmod(0o755)
    try:
        writable_units = {
            str(unit.get("id") or "")
            for unit in queue.get("units", [])
            if unit.get("type") == "run" and unit.get("status") == "running"
        }
        for unit_id in writable_units:
            artifact_dir = artifact_root / unit_id
            artifact_dir.mkdir(exist_ok=True)
            artifact_dir.chmod(0o755)
        for child in artifact_root.iterdir():
            if child.name not in writable_units:
                seal_artifact_tree(child)
    finally:
        artifact_root.chmod(0o555)


def command_execute_run(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    with queue_lock(project_root):
        queue = load_queue(project_root)
        units = unit_by_id(queue)
        if args.unit not in units:
            raise SystemExit(f"unknown unit: {args.unit}")
        unit = units[args.unit]
        require_unit_type(unit, "run", "execute-run")
        reject_if_claim_lost(unit, args.worker)
        if unit.get("status") != "running":
            print(json.dumps({
                "status": "rejected",
                "reason": "run_unit_not_running",
                "unit": unit.get("id"),
                "unit_status": unit.get("status"),
            }, ensure_ascii=False))
            raise SystemExit(5)
        contract_gaps, environment = run_command_contract(project_root, unit, argv)
        if contract_gaps:
            print(json.dumps({
                "status": "rejected",
                "reason": "run_command_contract_invalid",
                "unit": unit.get("id"),
                "missing": contract_gaps,
            }, ensure_ascii=False))
            raise SystemExit(4)
        cycle = int(unit.get("cycle", 0) or 0)
        stage = expected_run_stage(unit)
        results = project_root / "results"
        run_log = results / "run.log"
        artifact_root = results / "run_artifacts"
        artifact_dir = artifact_root / str(args.unit)
        results.mkdir(parents=True, exist_ok=True)
        run_log.touch(exist_ok=True)
        prepare_run_artifact_namespaces(project_root, queue)
        attempt_number = 1 + max(
            (
                int(match.group(1))
                for path in artifact_dir.glob("attempt-*.log")
                if (match := re.fullmatch(r"attempt-(\d+)\.log", path.name))
            ),
            default=0,
        )
        attempt = artifact_dir / f"attempt-{attempt_number}.log"
        before_bytes = run_log.stat().st_size
        before_sha256 = sha256_file(run_log)
        started_at = now_iso()

    with attempt.open("xb") as handle:
        completed = subprocess.run(
            argv,
            cwd=project_root,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    finished_at = now_iso()
    after_bytes = run_log.stat().st_size
    after_sha256 = sha256_file(run_log)
    contract_gaps = []
    if after_bytes < before_bytes:
        contract_gaps.append("共享 run.log 被截断")
    elif before_bytes:
        digest = hashlib.sha256()
        with run_log.open("rb") as handle:
            remaining = before_bytes
            while remaining:
                chunk = handle.read(min(1 << 20, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
        if remaining or digest.hexdigest() != before_sha256:
            contract_gaps.append("共享 run.log 的既有前缀被改写")
    with queue_lock(project_root):
        queue = load_queue(project_root)
        unit = unit_by_id(queue).get(args.unit)
        if unit is None or unit.get("status") != "running":
            contract_gaps.append("run unit 在命令结束前失去 running 状态")
        else:
            reject_if_claim_lost(unit, args.worker)
            contract_gaps.extend(run_artifact_namespace_gaps(project_root, queue, unit))
        event = append_engine_event(
            project_root,
            "run_execution",
            unit=args.unit,
            cycle=cycle,
            stage=stage,
            source="execute-run",
            started_at=started_at,
            finished_at=finished_at,
            exit_code=completed.returncode,
            argv=argv,
            cwd=".",
            environment=environment,
            artifact_dir=str(artifact_dir.relative_to(project_root)),
            run_log={
                "path": str(run_log.relative_to(project_root)),
                "before_bytes": before_bytes,
                "before_sha256": before_sha256,
                "after_bytes": after_bytes,
                "after_sha256": after_sha256,
            },
            attempt={
                "path": str(attempt.relative_to(project_root)),
                "sha256": sha256_file(attempt),
            },
            contract_gaps=contract_gaps,
        )
        append_decision(
            project_root,
            f"event=run_executed unit={args.unit} stage={stage} "
            f"exit_code={completed.returncode} event_hash={event['event_hash']}",
        )
    status = "completed" if completed.returncode == 0 and not contract_gaps else "rejected"
    print(json.dumps({
        "status": status,
        "unit": args.unit,
        "stage": stage,
        "exit_code": completed.returncode,
        "execution_event_hash": event["event_hash"],
        "attempt": str(attempt.relative_to(project_root)),
        "contract_gaps": contract_gaps,
    }, ensure_ascii=False))
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    if contract_gaps:
        raise SystemExit(4)


def project_permission_gaps(project_root: Path) -> list[str]:
    settings_path = project_root / ".claude" / "settings.json"
    if not settings_path.exists() and not settings_path.is_symlink():
        return []
    if settings_path.is_symlink() or settings_path.parent.is_symlink():
        return ["项目级 Claude 权限文件不得是符号链接：.claude/settings.json"]
    try:
        settings = read_json(settings_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"项目级 Claude 权限文件无法核验：{exc}"]
    if not isinstance(settings, dict):
        return ["项目级 Claude 权限文件顶层必须是对象"]
    permissions = settings.get("permissions")
    allow = permissions.get("allow") if isinstance(permissions, dict) else []
    if not isinstance(allow, list):
        return ["项目级 Claude permissions.allow 必须是数组"]
    gaps = []
    for raw in allow:
        entry = str(raw or "").strip()
        if entry == "Bash(*)" or re.match(r"^Bash\(\s*(?:rm\s+-[^)]*r|sudo(?:\s|\)))", entry):
            gaps.append(f"项目级 Claude 权限包含危险 Bash 放行：{entry}")
    return gaps


def run_receipt_evidence(
    project_root: Path,
    unit: dict[str, Any],
    queue: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    receipt_path = run_receipt_path(project_root, unit)
    if not receipt_path.exists():
        return [f"run receipt 不存在：{receipt_path.relative_to(project_root)}"], {}
    try:
        receipt = read_json(receipt_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"run receipt 无法读取：{exc}"], {}
    gaps: list[str] = []
    if receipt.get("schema_version") != RUN_RECEIPT_SCHEMA_VERSION:
        gaps.append("run receipt schema_version 无效")
    if receipt.get("unit") != unit.get("id"):
        gaps.append("run receipt unit 与目标单元不一致")
    if receipt.get("cycle") != int(unit.get("cycle", 0) or 0):
        gaps.append("run receipt cycle 与目标单元不一致")
    if receipt.get("status") != "completed" or receipt.get("exit_code") != 0:
        gaps.append("run receipt 没有记录 completed + exit_code=0")
    if not artifact_belongs_to(receipt_path, unit):
        gaps.append("run receipt 早于本次 unit 启动")
    started = parse_iso(receipt.get("started_at"))
    finished = parse_iso(receipt.get("finished_at"))
    unit_started = parse_iso(unit.get("started_at"))
    if not started or not finished or finished < started:
        gaps.append("run receipt 起止时间无效")
    elif unit_started and started < unit_started:
        gaps.append("run receipt 属于本次 unit 启动前的执行")
    execution_gaps, execution = run_execution_evidence(project_root, unit, receipt)
    gaps.extend(execution_gaps)
    if queue is not None:
        gaps.extend(run_artifact_namespace_gaps(project_root, queue, unit))

    immutable_root = project_root / "results" / "run_artifacts" / str(unit.get("id"))
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        gaps.append("run receipt 没有不可变原始 artifact")
        artifacts = []
    checked_artifacts: list[dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            gaps.append("run receipt artifact 不是对象")
            continue
        path, error = project_relative_path(project_root, item.get("path"))
        if error or path is None:
            gaps.append(error)
            continue
        try:
            path.relative_to(immutable_root)
        except ValueError:
            gaps.append(
                f"run artifact 不在 results/run_artifacts/{unit.get('id')}/ 下："
                f"{path.relative_to(project_root)}"
            )
            continue
        if not path.is_file():
            gaps.append(f"run artifact 不存在：{path.relative_to(project_root)}")
            continue
        actual_hash = sha256_file(path)
        if item.get("sha256") != actual_hash:
            gaps.append(f"run artifact 哈希不匹配：{path.relative_to(project_root)}")
            continue
        checked_artifacts.append({
            "path": str(path.relative_to(project_root)),
            "sha256": actual_hash,
        })

    listed_paths = {item["path"] for item in checked_artifacts}
    actual_paths = {
        str(path.relative_to(project_root))
        for path in immutable_root.rglob("*")
        if path.is_file()
    } if immutable_root.is_dir() else set()
    for omitted in sorted(actual_paths - listed_paths):
        gaps.append(f"run receipt 漏列 immutable artifact：{omitted}")

    summary = receipt.get("summary")
    if not isinstance(summary, dict):
        gaps.append("run receipt 没有 summary 哈希")
    else:
        summary_path, error = project_relative_path(project_root, summary.get("path"))
        if error or summary_path is None:
            gaps.append(error)
        else:
            relative_summary = str(summary_path.relative_to(project_root))
            try:
                summary_path.relative_to(immutable_root)
            except ValueError:
                gaps.append(
                    f"run summary 不在 results/run_artifacts/{unit.get('id')}/ 下："
                    f"{relative_summary}"
                )
            if not summary_path.is_file():
                gaps.append(f"run summary 不存在：{relative_summary}")
            elif summary.get("sha256") != sha256_file(summary_path):
                gaps.append(f"run summary 哈希不匹配：{relative_summary}")
            if not any(
                item.get("path") == relative_summary
                and item.get("sha256") == summary.get("sha256")
                for item in checked_artifacts
            ):
                gaps.append("run summary 没有列入已核验的 immutable artifacts")

    evidence = {
        "receipt": str(receipt_path.relative_to(project_root)),
        "receipt_sha256": sha256_file(receipt_path),
        "artifacts": checked_artifacts,
        "summary": summary if isinstance(summary, dict) else {},
        "execution": execution,
    }
    return gaps, evidence


def read_review_report(project_root: Path) -> dict[str, Any]:
    path = project_root / "review.md"
    if not path.exists():
        return {"present": False}
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\Z)", text, re.DOTALL)
    fields: dict[str, str] = {}
    if match:
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[normalize_field(key)] = value.strip().strip("'\"")

    def integer(name: str) -> int | None:
        try:
            return int(fields.get(name, ""))
        except ValueError:
            return None

    return {
        "present": True,
        "structured": bool(match),
        "unit": fields.get("unit", ""),
        "cycle": integer("cycle"),
        "blockers_count": integer("blockers count"),
        "body_blockers_count": len(
            re.findall(
                r"(?mi)^\s*-\s*\[B\d+\]\s*<severity:[^>\n]+>",
                text,
            )
        ),
        "reviewer": fields.get("reviewer", ""),
        "model": fields.get("model", ""),
        "model_identity": fields.get("model identity", ""),
    }


def review_evidence(project_root: Path, unit: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    path = project_root / "review.md"
    report = read_review_report(project_root)
    gaps: list[str] = []
    if not report.get("present"):
        gaps.append("review.md 不存在")
    elif not artifact_belongs_to(path, unit):
        gaps.append("review.md 早于本次 review unit 启动")
    if not report.get("structured"):
        gaps.append("review.md 没有结构化 frontmatter")
    if report.get("unit") != unit.get("id"):
        gaps.append("review.md unit 与目标单元不一致")
    if report.get("cycle") != int(unit.get("cycle", 0) or 0):
        gaps.append("review.md cycle 与目标单元不一致")
    if report.get("blockers_count") != 0:
        gaps.append(f"review.md blockers_count={report.get('blockers_count')}，不能放行")
    if report.get("body_blockers_count") != report.get("blockers_count"):
        gaps.append(
            "review.md 正文 blocker 数与 frontmatter 不一致："
            f"正文={report.get('body_blockers_count')}，"
            f"blockers_count={report.get('blockers_count')}"
        )
    if not report.get("reviewer") or not report.get("model") or not report.get("model_identity"):
        gaps.append("review.md 缺 reviewer/model/model_identity provenance")
    evidence = {}
    if path.exists():
        evidence = {
            "artifact": "review.md",
            "artifact_sha256": sha256_file(path),
            "artifact_mtime_ns": path.stat().st_mtime_ns,
            "reviewer": report.get("reviewer"),
            "model": report.get("model"),
            "model_identity": report.get("model_identity"),
        }
    return gaps, evidence


def review_receipt_payload_gaps(
    payload: Any,
    unit: dict[str, Any],
    artifact: Path | None = None,
    report: dict[str, Any] | None = None,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["review producer receipt 不是对象"]
    gaps: list[str] = []
    unit_id = str(unit.get("id") or "")
    cycle = int(unit.get("cycle", 0) or 0)
    if payload.get("schema_version") != REVIEW_RECEIPT_SCHEMA_VERSION:
        gaps.append("review producer receipt schema_version 无效")
    if payload.get("unit") != unit_id:
        gaps.append("review producer receipt unit 与目标单元不一致")
    if payload.get("cycle") != cycle:
        gaps.append("review producer receipt cycle 与目标单元不一致")
    if payload.get("artifact") != "review.md":
        gaps.append("review producer receipt artifact 不是 review.md")
    receipt_sha = str(payload.get("artifact_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", receipt_sha) is None:
        gaps.append("review producer receipt artifact_sha256 无效")
    if artifact is not None:
        artifact_sha = sha256_file(artifact) if artifact.is_file() else ""
        if receipt_sha != artifact_sha:
            gaps.append("review producer receipt artifact SHA256 与当前 review.md 不一致")
    try:
        uuid.UUID(str(payload.get("request_id") or ""))
    except ValueError:
        gaps.append("review producer receipt request_id 不是 UUID")
    producer_pid = payload.get("producer_pid")
    if not isinstance(producer_pid, int) or producer_pid <= 0:
        gaps.append("review producer receipt producer_pid 无效")
    issued_at = parse_iso(payload.get("issued_at"))
    started_at = parse_iso(unit.get("started_at"))
    if issued_at is None:
        gaps.append("review producer receipt issued_at 无效")
    elif started_at is None or issued_at < started_at:
        gaps.append("review producer receipt issued_at 早于本次 review 单元启动")
    elif issued_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        gaps.append("review producer receipt issued_at 晚于当前时间")
    if payload.get("reviewer") != "gemini-mcp-tool":
        gaps.append("review producer receipt reviewer 不是 gemini-mcp-tool")
    if not str(payload.get("model") or "").strip():
        gaps.append("review producer receipt 缺少 model")
    if not str(payload.get("model_identity") or "").strip():
        gaps.append("review producer receipt 缺少 model_identity")
    if not isinstance(payload.get("blockers_count"), int):
        gaps.append("review producer receipt blockers_count 无效")
    if report is not None:
        for field in ("reviewer", "model", "model_identity", "blockers_count"):
            if payload.get(field) != report.get(field):
                gaps.append(f"review producer receipt {field} 与 review.md 不一致")
    return gaps


def review_receipt_evidence(
    project_root: Path,
    unit: dict[str, Any],
    artifact: Path | None = None,
    report: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    events, problems = read_engine_events(project_root)
    candidates = [
        event
        for event in events
        if event.get("kind") == "review_report_receipt"
        and event.get("unit") == unit.get("id")
        and event.get("cycle") == int(unit.get("cycle", 0) or 0)
    ]
    if not candidates:
        return [*problems, "当前 review 单元没有 MCP producer receipt"], {}
    event = candidates[-1]
    gaps = list(problems)
    if event.get("source") != REVIEW_RECEIPT_SOURCE:
        gaps.append("review producer receipt event source 无效")
    event_at = parse_iso(event.get("at"))
    started_at = parse_iso(unit.get("started_at"))
    if not event_at or not started_at or event_at < started_at:
        gaps.append("review producer receipt event 早于本次 review 单元启动")
    payload = event.get("receipt")
    gaps.extend(review_receipt_payload_gaps(payload, unit, artifact, report))
    receipt = payload if isinstance(payload, dict) else {}
    return gaps, {
        "event_hash": event.get("event_hash"),
        "request_id": receipt.get("request_id"),
        "artifact_sha256": receipt.get("artifact_sha256"),
        "reviewer": receipt.get("reviewer"),
        "model": receipt.get("model"),
        "model_identity": receipt.get("model_identity"),
        "blockers_count": receipt.get("blockers_count"),
    }


def report_belongs_to(project_root: Path, unit: dict[str, Any]) -> bool:
    return artifact_belongs_to(blind_review_path(project_root), unit)


# `Reviewer Count` / `Average Rating` / `Independent Review Rating` / `Review Decision`
# 是 #241 那份产物里实际出现的改名，其余几条是同一改法的近邻。别名只收同义改写，不猜
# 语义：`Reviewer Count` 就是评审数，但 `Confidence` 不是分数。
# decision 的值不做清洗，它只进 unit["result"] 和 decisions.log 给人看，不参与判定。
BLIND_REVIEW_ALIASES = {
    "avg_rating": ("average rating", "overall rating", "independent review rating"),
    "n_reviews": ("reviewer count", "review count", "number of reviews"),
    "decision": ("review decision", "recommendation"),
    "top_weaknesses": ("weaknesses", "key weaknesses"),
    "calibration_gap": (),
}


def read_blind_review_report(project_root: Path) -> dict[str, Any]:
    path = blind_review_path(project_root)
    text = path.read_text(errors="replace") if path.exists() else ""

    def field(name: str) -> list[str]:
        return parse_bullet_field(text, name, BLIND_REVIEW_ALIASES[name])

    rating_values = field("avg_rating")
    n_values = field("n_reviews")
    decision_values = field("decision")
    weaknesses = field("top_weaknesses")
    gap_values = field("calibration_gap")

    def to_float(values: list[str]) -> float | None:
        if not values:
            return None
        # `7.8/10`、`7.8 (high)` 都出现过，取第一个数字。
        match = re.search(r"-?\d+(?:\.\d+)?", values[0])
        return float(match.group()) if match else None

    return {
        # 文件在不在要报出来。缺文件和「文件在但 n_reviews=0」是两种失败：前者多半是
        # reviewer 还没跑完，后者是盲审真的没做成。之前两者都塌成 avg_rating=None，
        # 于是「还没跑完」被当成「做不了」直接放行 close。
        "present": path.exists(),
        "avg_rating": to_float(rating_values),
        "n_reviews": int(to_float(n_values) or 0),
        # 「reviewer 写下了零评审」和「一个字段都读不出来」是两件事。前者是它如实报告
        # 失败，后者是格式没对上，账本上不该记成同一种收场（#241）。
        "n_reviews_declared": bool(n_values),
        "decision": decision_values[0].lower() if decision_values else "",
        "top_weaknesses": weaknesses,
        "calibration_gap": to_float(gap_values),
    }


def merge_critic_into_report(report: dict[str, Any], critic: dict[str, Any]) -> dict[str, Any]:
    merged = dict(report)
    verdict = str(critic.get("verdict") or "").strip().lower()
    required_focus = list(critic.get("required_next_focus") or [])
    optional_focus = list(critic.get("optional_next_focus") or [])

    if verdict in {"needs_revision", "needs_more_research", "revise", "rerun"} and not merged["next_focus"]:
        merged["next_focus"] = (required_focus or optional_focus)[:3]

    if verdict in {"finish_ok", "approve"} and not merged["stop_reason"]:
        merged["stop_reason"] = critic.get("stop_reason") or "external_critic_finish_ok"

    if verdict and verdict not in {"finish_ok", "approve"}:
        merged["critic_verdict"] = verdict
    return merged


def next_cycle_units(cycle: int, previous_analysis_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"planner_revise_from_results_c{cycle}",
            "cycle": cycle,
            "type": "planning",
            "stage": "iteration",
            "status": "pending",
            "blocked_by": previous_analysis_id,
        },
        {
            "id": f"coder_apply_result_focus_c{cycle}",
            "cycle": cycle,
            "type": "coding",
            "stage": "iteration",
            "status": "pending",
            "blocked_by": f"planner_revise_from_results_c{cycle}",
        },
        {
            "id": f"review_iteration_c{cycle}",
            "cycle": cycle,
            "type": "review",
            "stage": "iteration",
            "status": "pending",
            "blocked_by": f"coder_apply_result_focus_c{cycle}",
        },
        {
            "id": f"runner_rerun_c{cycle}",
            "cycle": cycle,
            "type": "run",
            "stage": "iteration",
            "status": "pending",
            "blocked_by": f"review_iteration_c{cycle}",
        },
        {
            "id": f"result_analysis_c{cycle}",
            "cycle": cycle,
            "type": "result-analysis",
            "stage": "iteration",
            "status": "pending",
            "blocked_by": f"runner_rerun_c{cycle}",
        },
    ]


def critic_unit_id(analysis_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", analysis_id).strip("_")
    return f"external_critic_after_{safe}"


def rewire_analysis_successors(queue: dict[str, Any], analysis_id: str, critic_id: str) -> None:
    for unit in queue.get("units", []):
        if unit.get("id") == critic_id:
            continue
        blocker = unit.get("blocked_by")
        if blocker == analysis_id:
            unit["blocked_by"] = critic_id
        elif isinstance(blocker, list):
            unit["blocked_by"] = [critic_id if str(item) == analysis_id else item for item in blocker]


def append_critic(queue: dict[str, Any], analysis_id: str) -> bool:
    unit_id = critic_unit_id(analysis_id)
    if unit_id in unit_by_id(queue):
        rewire_analysis_successors(queue, analysis_id, unit_id)
        return False
    units = unit_by_id(queue)
    analysis_unit = units.get(analysis_id, {})
    queue["units"].append(
        {
            "id": unit_id,
            "cycle": int(analysis_unit.get("cycle", queue.get("current_cycle", 0)) or 0),
            "type": "critic",
            "stage": analysis_unit.get("stage", "unknown"),
            "status": "pending",
            "blocked_by": analysis_id,
            "artifact": "critic.md",
        }
    )
    rewire_analysis_successors(queue, analysis_id, unit_id)
    return True


def blind_review_unit_id(blocked_by: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", blocked_by).strip("_")
    return f"blind_review_after_{safe}"


def append_blind_review(queue: dict[str, Any], blocked_by: str) -> str | None:
    """在 close 之前插入无记忆盲审单元。

    同一个 blocker 只插一次（防重复）；盲审触发的修订轮结束后允许再次盲审
    （新 blocker → 新 unit id），以便对修订后的结果重新打分。
    """
    unit_id = blind_review_unit_id(blocked_by)
    if unit_id in unit_by_id(queue):
        return None
    blocker_unit = unit_by_id(queue).get(blocked_by, {})
    queue["units"].append(
        {
            "id": unit_id,
            "cycle": int(blocker_unit.get("cycle", queue.get("current_cycle", 0)) or 0),
            "type": "blind-review",
            "stage": blocker_unit.get("stage", "unknown"),
            "status": "pending",
            "blocked_by": blocked_by,
            "artifact": "blind_review.md",
        }
    )
    return unit_id


def append_close(queue: dict[str, Any], blocked_by: str, reason: str) -> bool:
    close_id = "close_if_done"
    if close_id in unit_by_id(queue):
        return False
    queue["units"].append(
        {
            "id": close_id,
            "cycle": queue.get("current_cycle", 0),
            "type": "close",
            "status": "pending",
            "blocked_by": blocked_by,
            "reason": reason,
        }
    )
    return True


def append_next_cycle(
    queue: dict[str, Any],
    project_root: Path,
    critic_unit: dict[str, Any],
) -> str:
    previous_analysis_id = str(critic_unit.get("blocked_by") or "")
    report = read_cycle_report(project_root)
    critic = read_critic_report(project_root)
    report = merge_critic_into_report(report, critic)
    source_cycle = int(critic_unit.get("cycle", 0) or 0)
    current_cycle = int(queue.get("current_cycle", 0) or 0)
    max_cycles = int(queue.get("max_cycles", DEFAULT_MAX_CYCLES) or DEFAULT_MAX_CYCLES)
    cycle_status = queue.setdefault("cycle_status", {})

    # Pilot critique gates the main experiment inside cycle 0. It must not create a
    # revision cycle before planner_scale_up/main_result_analysis have run.
    if critic_unit.get("stage") == "pilot":
        cycle_status[str(source_cycle)] = {
            "stage": "initial",
            "status": "running",
            "ended_at": None,
            "key_findings": report["key_findings"],
            "next_focus": report["next_focus"],
            "stop_reason": report["stop_reason"],
            "critic_verdict": report.get("critic_verdict") or critic.get("verdict") or None,
        }
        return "pilot_critic_complete"

    if source_cycle != current_cycle:
        raise RuntimeError(
            f"critic unit cycle {source_cycle} cannot advance current cycle {current_cycle}"
        )

    cycle_status[str(source_cycle)] = {
        "stage": "initial" if source_cycle == 0 else "iteration",
        "status": "done",
        "ended_at": now_iso(),
        "key_findings": report["key_findings"],
        "next_focus": report["next_focus"],
        "stop_reason": report["stop_reason"],
        "critic_verdict": report.get("critic_verdict") or critic.get("verdict") or None,
    }

    if not report["next_focus"]:
        # 收尾前强制过一次无记忆盲审（挤自评水分）；盲审结果由 after-blind-review 裁决
        blind_id = append_blind_review(queue, previous_analysis_id)
        if blind_id:
            return f"blind_review_appended:{blind_id}"
        appended = append_close(queue, previous_analysis_id, report["stop_reason"] or "no_next_focus")
        return "close_appended" if appended else "close_already_present"

    if source_cycle + 1 >= max_cycles:
        blind_id = append_blind_review(queue, previous_analysis_id)
        if blind_id:
            return f"blind_review_appended:{blind_id}"
        appended = append_close(queue, previous_analysis_id, "max_cycles_reached")
        return "max_cycles_close_appended" if appended else "max_cycles_close_already_present"

    next_cycle = source_cycle + 1
    existing = {unit["id"] for unit in queue.get("units", [])}
    new_units = [unit for unit in next_cycle_units(next_cycle, previous_analysis_id) if unit["id"] not in existing]
    queue["units"].extend(new_units)
    queue["current_cycle"] = next_cycle
    cycle_status[str(next_cycle)] = {
        "stage": "iteration",
        "status": "pending",
        "started_at": None,
        "ended_at": None,
        "derived_from": source_cycle,
        "focus": report["next_focus"],
        "key_findings": [],
        "next_focus": [],
        "stop_reason": None,
    }
    return f"cycle_{next_cycle}_appended:{len(new_units)}"


def command_init(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    path = queue_path(project_root)
    existed = path.exists()
    problem = incomplete_workflow_state(project_root)
    if problem is not None:
        print(json.dumps(problem, ensure_ascii=False))
        raise SystemExit(5)
    queue = load_queue(project_root)
    # 已有队列上 init 只做幂等校验。resume 会重跑 init，所以相同参数必须放行；但一次
    # 跑批里三条 queue_initialized、最后一条把 max_cycles 从 3 改成 2，意味着后来的会话
    # 静默换掉了这次运行的参数——进度还在，判据变了。
    if existed and args.max_cycles is not None and args.max_cycles != queue["max_cycles"]:
        print(json.dumps({
            "status": "rejected", "reason": "max_cycles_would_change",
            "current": queue["max_cycles"], "requested": args.max_cycles,
            "hint": "已有队列的参数不能被 init 改写。需要改变预算时新建 project root，"
                    "不要编辑 queue 或 engine mirror。",
        }, ensure_ascii=False))
        raise SystemExit(5)
    if args.max_cycles is not None:
        queue["max_cycles"] = args.max_cycles
    queue["completion_authority_version"] = COMPLETION_AUTHORITY_VERSION
    write_queue(project_root, queue)
    append_engine_event(
        project_root,
        "engine_initialized",
        max_cycles=queue["max_cycles"],
        engine_seq=queue["engine_seq"],
    )
    append_decision(project_root, f"event=queue_initialized path={path} max_cycles={queue['max_cycles']}")
    print(json.dumps({"status": "ok", "queue_path": str(path), "counts": queue_counts(queue)}, ensure_ascii=False))


def recover_unfinished_blind_review(project_root: Path, queue: dict[str, Any]) -> str | None:
    """队列排空却没有 close 时，把那个名不副实的盲审单元退回去。

    只在三件事同时成立时动手：没有 close 单元、有盲审单元被标成 done、产物不存在或
    `n_reviews < MIN_BLIND_REVIEWS`。

    显式降级过的那种不碰——`blind_review_unavailable` 是等过预算之后记录下来的结论，
    把它拉回来会变成无限重排。
    """
    units = queue.get("units", [])
    if any(u.get("type") == "close" for u in units):
        return None
    report = read_blind_review_report(project_root)
    if report["present"] and report["n_reviews"] >= MIN_BLIND_REVIEWS:
        return None
    for unit in units:
        # 只恢复 done。`failed` / `skipped` 是有人（或引擎）明确下过的结论，把它们改回
        # pending 等于吞掉 reviewer 的真实失败，而 Ralph 循环会一直重跑它。
        if unit.get("type") == "blind-review" and unit.get("status") == "done":
            unit["status"] = "pending"
            unit["ended_at"] = None
            return str(unit.get("id"))
    return None


def command_next_prompt(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    path = queue_path(project_root)
    queue = load_queue(project_root)
    unit = next_unit(queue)
    counts = queue_counts(queue)
    if unit is None:
        # 队列排空、没有 close，而某个盲审单元被标成 done 却没有产物：这是两次实跑的
        # 终态，运行既不结束也无法继续。原来只打印一段让模型自己判断的提示，而恢复
        # 不该依赖模型照做——确定性地把那个单元退回 pending，下一次 next-prompt 就能
        # 领走它。
        recovered = recover_unfinished_blind_review(project_root, queue)
        if recovered:
            write_queue(project_root, queue)
            append_decision(project_root,
                            f"event=blind_review_recovered unit={recovered} "
                            f"reason=queue_drained_without_close")
            print(f"队列已空但没有 close，且 {recovered} 没有有效盲审产物。"
                  f"已把它退回 pending，重新运行 next-prompt 领取；不要输出 AUTORESEARCH_DONE。")
            return
        # 反绕过：没有任何盲审单元跑完就想收尾（例如假设证伪后大批 skip 直接 close），
        # 强制补插一个盲审——负结果也要冷启动打分，这正是挤水分机制存在的意义。
        has_blind_done = any(
            u.get("type") == "blind-review" and u.get("status") in TERMINAL_STATUSES
            for u in queue.get("units", [])
        )
        terminal_analysis = [
            u["id"]
            for u in queue.get("units", [])
            if u.get("type") in ("result-analysis", "critic") and u.get("status") == "done"
        ]
        if not has_blind_done and terminal_analysis:
            blind_id = append_blind_review(queue, terminal_analysis[-1])
            if blind_id:
                write_queue(project_root, queue)
                append_decision(
                    project_root,
                    f"event=blind_review_enforced unit={blind_id} reason=close_without_blind_review",
                )
                print(
                    f"检测到收尾前未做无记忆盲审，已强制插入 {blind_id}。"
                    "重新运行 next-prompt 领取该单元；不要输出 AUTORESEARCH_DONE。"
                )
                return
        write_queue(project_root, queue)
        print(
            "没有可执行的 next_unit。请读取 workflow_queue.json/state.md："
            "如果 close_if_done 已完成，最后一行输出 <promise>AUTORESEARCH_DONE</promise>；"
            "如果存在 blocked/failed，写明 waiting_for=user 并停止。"
        )
        return

    was_running = unit.get("status") == "running"
    unit["status"] = "running"
    # 显式退回 pending 后再领取要刷新时间边界。恢复已在跑的单元则属于同一次
    # 执行；重置 started_at 会把刚落盘的 receipt 变成 stale，使 run 单元无限重试。
    if not was_running or parse_iso(unit.get("started_at")) is None:
        unit["started_at"] = now_iso()
    queue["current_unit"] = unit["id"]
    write_queue(project_root, queue)
    append_decision(
        project_root,
        f"event=next_unit_selected unit={unit['id']} cycle={unit.get('cycle')} type={unit.get('type')}",
    )
    print(
        "\n".join(
            [
                "继续 AutoResearch 工作流。",
                f"project_root = {project_root}",
                f"workflow_queue = {path}",
                f"next_unit = {unit['id']}",
                f"cycle = {unit.get('cycle', 0)}",
                f"type = {unit.get('type', 'unknown')}",
                f"stage = {unit.get('stage', 'none')}",
                f"queue_counts = {counts}",
                "",
                "只执行这个 next_unit，不要连续执行后续 unit。",
                "执行前读取 state.md、workflow_queue.json、decisions.log 最近事件。",
                "执行后只通过本提示给出的 engine 命令回写 unit；不要直接编辑 queue、engine mirror "
                "或 workflow_events.jsonl。按需更新 state.md 和 decisions.log。",
                f"普通单元完成命令：{ENGINE_COMMAND} complete --project-root {project_root} "
                f"--unit {unit['id']} --status done --result \"<一句话>\"",
                "run 单元先写 results/run_receipts/<unit>.json；review 单元先写绑定当前 "
                "unit/cycle 的 review.md frontmatter。",
                *adjudication_hints(project_root, unit["id"]),
                "如果还有 pending，不要输出 AUTORESEARCH_DONE。",
            ]
        )
    )


def require_unit_type(unit: dict[str, Any], expected: str, command: str) -> None:
    """裁决命令只对自己那型的单元有效（#151）。

    sentinel 实测：after-result-analysis 收下了 type=run 的 run_pilot_experiment，
    critic 链就此被拉起，整条工作流被绕空。类型不符是调用方的错误，不是可以顺手
    接受的输入。
    """
    actual = str(unit.get("type") or "")
    if actual != expected:
        print(json.dumps({"status": "rejected", "reason": "unit_type_mismatch",
                          "command": command, "expected": expected, "actual": actual,
                          "unit": unit.get("id")}, ensure_ascii=False))
        raise SystemExit(5)


def reject_if_claim_lost(unit: dict[str, Any], worker: str) -> None:
    """租约被回收之后，原 worker 的迟到回写不算数，否则两个 worker 同时写一个单元。"""
    holder = unit.get("claimed_by")
    if holder and worker and holder != worker:
        print(json.dumps({"status": "rejected", "reason": "claim_lost",
                          "current_holder": holder}, ensure_ascii=False))
        raise SystemExit(3)


def drop_claim(unit: dict[str, Any]) -> None:
    """单元的状态被改写之后租约就该还回去，无论它落到终态还是退回 pending。"""
    unit.pop("claimed_by", None)
    unit.pop("lease_expires_at", None)


def open_adjudication(args: argparse.Namespace, expected: str) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    """Open an adjudication only for its running unit and current lease owner."""
    project_root = Path(args.project_root).resolve()
    path = queue_path(project_root)
    queue = load_queue(project_root)
    units = unit_by_id(queue)
    if args.unit not in units:
        raise SystemExit(f"unknown unit: {args.unit}")
    unit = units[args.unit]
    require_unit_type(unit, expected, ADJUDICATION[expected]["command"])
    reject_if_claim_lost(unit, getattr(args, "worker", ""))
    if unit.get("status") != "running":
        print(json.dumps({
            "status": "rejected",
            "reason": "adjudication_unit_not_running",
            "command": ADJUDICATION[expected]["command"],
            "unit": unit.get("id"),
            "unit_status": unit.get("status"),
        }, ensure_ascii=False))
        raise SystemExit(5)
    return project_root, path, queue, unit


def defer_for_artifact(
    project_root: Path,
    queue: dict[str, Any],
    unit: dict[str, Any],
    *,
    event: str,
    outcome: str,
    present: bool,
    stale: bool,
    unparsable: bool,
) -> None:
    same_attempt_retry = present and unparsable and not stale
    unit["status"] = "running" if same_attempt_retry else "pending"
    unit["ended_at"] = None
    if not same_attempt_retry:
        drop_claim(unit)
    queue["iteration"] = int(queue.get("iteration", 0) or 0) + 1
    write_queue(project_root, queue)
    append_decision(
        project_root,
        f"event={event} unit={unit['id']} outcome={outcome} "
        f"present={present} stale={stale} unparsable={unparsable}",
    )
    spec = ADJUDICATION[str(unit["type"])]
    artifact = str(spec["artifact"])
    if same_attempt_retry:
        recovery = (
            f"保持当前领取，不要再次 claim；用规定的 producer 覆盖 {artifact}，"
            f"再运行 {spec['command']}。"
        )
    else:
        recovery = (
            f"重新运行 next-prompt/claim 领取 {unit['id']}，领取后重新写入 {artifact}，"
            f"再运行 {spec['command']}。"
        )
    print(json.dumps({
        "status": "retry_required" if same_attempt_retry else "pending",
        "outcome": outcome,
        "present": present,
        "stale": stale,
        "unparsable": unparsable,
        "required_artifact": artifact,
        "recovery": recovery,
        "counts": queue_counts(queue),
    }, ensure_ascii=False))


def command_after_result_analysis(args: argparse.Namespace) -> None:
    project_root, path, queue, unit = open_adjudication(args, "result-analysis")
    unit_id = unit["id"]
    decision = str(getattr(args, "decision", "") or "continue")
    report = read_cycle_report(project_root)
    artifact = adjudication_artifact_path(project_root, "result-analysis")
    stale = report["present"] and not artifact_belongs_to(artifact, unit)
    unparsable = (
        report["present"]
        and not stale
        and (
            not report["structured"]
            or decision not in ADJUDICATION["result-analysis"]["verdicts"]
            or (decision == "stop" and not (report["stop_reason"] or report["phase_2_skipped_reason"]))
        )
    )
    if not report["present"] or stale or unparsable:
        defer_for_artifact(
            project_root,
            queue,
            unit,
            event="after_result_analysis",
            outcome="result_analysis_pending_artifact",
            present=report["present"],
            stale=stale,
            unparsable=unparsable,
        )
        return
    unit["status"] = "done"
    unit["ended_at"] = now_iso()
    drop_claim(unit)
    unit["result"] = unit.get("result") or "result_analysis_done"
    # 分析的「继续/停止」是结构化裁决，在这里由引擎落账，skip-cycle 只认这份记录。
    # 曾经的判据是 state.md 的自由文本 stop_reason：E2E 实测 coordinator 写出
    # `none (continue to Phase 2)`，字面非空于是被当成停止依据，语义正好反转。
    # 自由文本只作展示，不再参与任何裁决。
    decided_at = now_iso()
    queue["last_analysis_decision"] = {
        "cycle": int(unit.get("cycle", 0) or 0),
        "unit": unit_id,
        "decision": decision,
        "artifact": str(ADJUDICATION["result-analysis"]["artifact"]),
        "artifact_mtime_ns": artifact.stat().st_mtime_ns,
        "at": decided_at,
    }
    appended = append_critic(queue, unit_id)
    outcome = "critic_appended" if appended else "critic_already_present"
    queue["iteration"] = int(queue.get("iteration", 0) or 0) + 1
    write_queue(project_root, queue)
    record_terminal_unit(
        project_root,
        unit,
        "after-result-analysis",
        {
            "artifact": str(artifact.relative_to(project_root)),
            "artifact_sha256": sha256_file(artifact),
            "artifact_mtime_ns": artifact.stat().st_mtime_ns,
            "decision": decision,
        },
    )
    append_decision(
        project_root,
        f"event=after_result_analysis unit={unit_id} outcome={outcome} "
        f"decision={decision} cycle={unit.get('cycle')} current_cycle={queue.get('current_cycle')}",
    )
    print(json.dumps({"status": "ok", "outcome": outcome, "decision": decision,
                      "counts": queue_counts(queue)}, ensure_ascii=False))


def process_working_directory(pid: int) -> Path | None:
    proc_cwd = Path(f"/proc/{pid}/cwd")
    if proc_cwd.exists():
        try:
            return proc_cwd.resolve(strict=True)
        except OSError:
            return None
    try:
        output = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    cwd = next((line[1:] for line in output.splitlines() if line.startswith("n")), "")
    try:
        return Path(cwd).resolve(strict=True) if cwd else None
    except OSError:
        return None


def bun_mcp_producer_gaps(
    label: str,
    script_name: str,
    claimed_pid: int | None = None,
) -> list[str]:
    """Confirm that a hidden persistence command is a direct child of its Bun MCP."""
    parent_pid = os.getppid()
    gaps: list[str] = []
    if claimed_pid is not None and claimed_pid != parent_pid:
        gaps.append(f"{label} producer_pid 与 engine 父进程不一致")
    try:
        parent = subprocess.run(
            ["ps", "-p", str(parent_pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        argv = shlex.split(parent)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return [*gaps, f"无法核验 {label} producer 进程：{exc}"]
    if not argv or Path(argv[0]).name != "bun":
        gaps.append(f"{label} producer 不是 Bun")
    expected_script = Path(__file__).resolve().with_name(script_name)
    producer_cwd = process_working_directory(parent_pid)
    script_matches = False
    for part in argv[1:]:
        candidate = Path(part)
        if candidate.name != expected_script.name:
            continue
        if not candidate.is_absolute():
            if producer_cwd is None:
                continue
            candidate = producer_cwd / candidate
        try:
            script_matches = candidate.resolve(strict=True) == expected_script
        except OSError:
            script_matches = False
        if script_matches:
            break
    if not script_matches:
        gaps.append(f"{label} producer 不是仓库内的 {script_name}")
    return gaps


def critic_receipt_producer_gaps(payload: dict[str, Any]) -> list[str]:
    """Confirm that the receipt arrived directly from the configured Bun MCP process."""
    return bun_mcp_producer_gaps(
        "critic receipt",
        "ar-external-critic-mcp.ts",
        payload.get("producer_pid"),
    )


def review_report_producer_gaps() -> list[str]:
    """Confirm that review.md arrived directly from the configured reviewer MCP."""
    return bun_mcp_producer_gaps("review report", "ar-gemini-review-mcp.ts")


def command_record_critic_receipt(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(json.dumps({
            "status": "rejected",
            "reason": "critic_receipt_invalid",
            "missing": [f"critic receipt stdin 不是 JSON：{exc}"],
        }, ensure_ascii=False))
        raise SystemExit(4) from None
    if not isinstance(payload, dict):
        print(json.dumps({
            "status": "rejected",
            "reason": "critic_receipt_invalid",
            "missing": ["critic receipt stdin 不是对象"],
        }, ensure_ascii=False))
        raise SystemExit(4)
    queue = load_queue(project_root)
    unit = unit_by_id(queue).get(str(payload.get("unit") or ""))
    gaps = critic_receipt_producer_gaps(payload)
    if unit is None or unit.get("type") != "critic":
        gaps.append("critic receipt 没有绑定现有 critic 单元")
    elif unit.get("status") != "running":
        gaps.append("critic receipt 只能登记到 running critic 单元")
    artifact = critic_path(project_root)
    report = read_critic_report(project_root)
    if unit is not None:
        if not artifact.is_file() or not artifact_belongs_to(artifact, unit):
            gaps.append("critic receipt 对应的 critic.md 缺失或不属于本次单元")
        else:
            gaps.extend(critic_receipt_payload_gaps(payload, unit, artifact, report))
    events, event_problems = read_engine_events(project_root)
    gaps.extend(event_problems)
    request_id = payload.get("request_id")
    if any(
        event.get("kind") == "critic_receipt"
        and isinstance(event.get("receipt"), dict)
        and event["receipt"].get("request_id") == request_id
        for event in events
    ):
        gaps.append("critic receipt request_id 已登记")
    if gaps:
        print(json.dumps({
            "status": "rejected",
            "reason": "critic_receipt_invalid",
            "missing": gaps,
        }, ensure_ascii=False))
        raise SystemExit(4)
    event = append_engine_event(
        project_root,
        "critic_receipt",
        unit=unit.get("id"),
        cycle=int(unit.get("cycle", 0) or 0),
        source=CRITIC_RECEIPT_SOURCE,
        receipt=payload,
    )
    print(json.dumps({
        "status": "ok",
        "request_id": request_id,
        "receipt_event_hash": event.get("event_hash"),
    }, ensure_ascii=False))


def command_record_review_report(args: argparse.Namespace) -> None:
    """Persist review.md only while its engine unit is still running."""
    project_root = Path(args.project_root).resolve()
    queue = load_queue(project_root)
    unit = unit_by_id(queue).get(args.unit)
    gaps = review_report_producer_gaps()
    if unit is None or unit.get("type") != "review":
        gaps.append("review report 没有绑定现有 review 单元")
    elif unit.get("status") not in {"running", "running-but-incomplete"}:
        gaps.append(f"review unit 已是 {unit.get('status')}，拒绝迟到 producer 覆盖产物")
    elif int(unit.get("cycle", 0) or 0) != args.cycle:
        gaps.append("review report cycle 与目标单元不一致")

    report = sys.stdin.read()
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\Z)", report, re.DOTALL)
    fields: dict[str, str] = {}
    if match:
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[normalize_field(key)] = value.strip().strip("'\"")
    if not report.strip() or not match:
        gaps.append("review report 没有结构化 frontmatter")
    if fields.get("unit") != args.unit:
        gaps.append("review report unit 与目标单元不一致")
    try:
        report_cycle = int(fields.get("cycle", ""))
    except ValueError:
        report_cycle = None
    if report_cycle != args.cycle:
        gaps.append("review report frontmatter cycle 与目标单元不一致")
    if not fields.get("reviewer") or not fields.get("model") or not fields.get("model identity"):
        gaps.append("review report 缺 reviewer/model/model_identity provenance")
    elif fields.get("reviewer") != "gemini-mcp-tool":
        gaps.append("review report reviewer 不是 gemini-mcp-tool")
    try:
        blockers_count = int(fields.get("blockers count", ""))
    except ValueError:
        blockers_count = None
    if blockers_count is None:
        gaps.append("review report blockers_count 无效")
    _, event_problems = read_engine_events(project_root)
    gaps.extend(event_problems)
    if gaps:
        print(json.dumps({
            "status": "rejected",
            "reason": "review_report_invalid",
            "missing": gaps,
        }, ensure_ascii=False))
        raise SystemExit(4)

    target = project_root / "review.md"
    temporary = project_root / f".review.md.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(report, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    receipt = {
        "schema_version": REVIEW_RECEIPT_SCHEMA_VERSION,
        "request_id": str(uuid.uuid4()),
        "issued_at": now_iso(),
        "producer_pid": os.getppid(),
        "unit": args.unit,
        "cycle": args.cycle,
        "artifact": "review.md",
        "artifact_sha256": sha256_file(target),
        "reviewer": fields.get("reviewer"),
        "model": fields.get("model"),
        "model_identity": fields.get("model identity"),
        "blockers_count": blockers_count,
    }
    event = append_engine_event(
        project_root,
        "review_report_receipt",
        unit=args.unit,
        cycle=args.cycle,
        source=REVIEW_RECEIPT_SOURCE,
        receipt=receipt,
    )
    print(json.dumps({
        "status": "recorded",
        "artifact": "review.md",
        "sha256": receipt["artifact_sha256"],
        "request_id": receipt["request_id"],
        "receipt_event_hash": event.get("event_hash"),
    }))


def command_after_critic(args: argparse.Namespace) -> None:
    project_root, path, queue, unit = open_adjudication(args, "critic")
    unit_id = unit["id"]
    # critic 产物要属于本轮，判据与盲审的 report_belongs_to 同构（#252）。E2E 实测：
    # Phase 2 critic 把 finish_ok 写进了别的文件名，这里读到的 critic.md 还是 Phase 1
    # 的 needs_revision，凭旧 verdict 追加了整条 c2 修订链。旧产物和「产物还没来」是
    # 同一回事：单元退回 pending 等真产物，什么都不追加。
    critic_file = adjudication_artifact_path(project_root, "critic")
    critic = read_critic_report(project_root)
    stale = critic_file.exists() and not artifact_belongs_to(critic_file, unit)
    verdict = str(critic.get("verdict") or "").strip().lower()
    binding_invalid = (
        critic_file.exists()
        and not stale
        and (
            critic.get("unit") != unit_id
            or critic.get("cycle") != int(unit.get("cycle", 0) or 0)
        )
    )
    unparsable = (
        critic_file.exists()
        and not stale
        and (
            binding_invalid
            or verdict not in ADJUDICATION["critic"]["verdicts"]
        )
    )
    if not critic_file.exists() or stale or unparsable:
        defer_for_artifact(
            project_root,
            queue,
            unit,
            event="after_critic",
            outcome="critic_pending_artifact",
            present=critic_file.exists(),
            stale=stale,
            unparsable=unparsable,
        )
        return
    receipt_gaps, receipt_evidence = critic_receipt_evidence(
        project_root,
        unit,
        critic_file,
        critic,
    )
    if receipt_gaps:
        print(json.dumps({
            "status": "rejected",
            "reason": "critic_receipt_invalid",
            "unit": unit_id,
            "missing": receipt_gaps,
        }, ensure_ascii=False))
        raise SystemExit(4)
    source_cycle = int(unit.get("cycle", 0) or 0)
    current_cycle = int(queue.get("current_cycle", 0) or 0)
    if unit.get("stage") != "pilot" and source_cycle != current_cycle:
        print(json.dumps({
            "status": "rejected",
            "reason": "critic_cycle_not_current",
            "unit": unit_id,
            "unit_cycle": source_cycle,
            "current_cycle": current_cycle,
        }, ensure_ascii=False))
        raise SystemExit(5)
    unit["status"] = "done"
    unit["ended_at"] = now_iso()
    drop_claim(unit)
    unit["result"] = unit.get("result") or "external_critic_done"
    outcome = append_next_cycle(queue, project_root, unit)
    queue["iteration"] = int(queue.get("iteration", 0) or 0) + 1
    write_queue(project_root, queue)
    record_terminal_unit(
        project_root,
        unit,
        "after-critic",
        {
            "artifact": str(critic_file.relative_to(project_root)),
            "artifact_sha256": sha256_file(critic_file),
            "artifact_mtime_ns": critic_file.stat().st_mtime_ns,
            "verdict": verdict,
            "producer_receipt": receipt_evidence,
        },
    )
    append_decision(
        project_root,
        f"event=after_critic unit={unit_id} outcome={outcome} current_cycle={queue.get('current_cycle')}",
    )
    print(json.dumps({"status": "ok", "outcome": outcome, "counts": queue_counts(queue)}, ensure_ascii=False))


def command_after_blind_review(args: argparse.Namespace) -> None:
    """盲审单元完成后裁决：达标/预算耗尽 → close；否则用评审弱点追加一轮修订。"""
    project_root, path, queue, unit = open_adjudication(args, "blind-review")
    unit_id = unit["id"]
    report = read_blind_review_report(project_root)
    # 报告还得属于这一轮。产物是全局单文件，修订之后会再排一个盲审单元，只看「文件在
    # 且 n_reviews 达到双评审门槛」的话第二轮能拿第一轮的报告过门（#127）。这道门原来长在
    # complete 上，而 #218 之后盲审单元只能由这里判完，门跟着搬过来：上一轮的报告和
    # 「报告还没来」是同一回事。
    stale = report["present"] and not report_belongs_to(project_root, unit)

    # 产物不在就不能把这个单元判完。实测过一次：单元被选中 21 秒后标 done 并 close，
    # 真正的 blind_review.md 五分钟后才落盘，内容是 avg_rating 3.0 / decision reject
    # ——按引擎自己的规则那次根本不该 close。
    # 「文件不在」和「文件在但一次评审都没有」都算产物还没到位，都值得等一轮：
    # SKILL 的契约字面写的就是「盲审 blocked（n_reviews=0）时重试 ≤ 1 次」。上一版只
    # 等前者，于是 n_reviews=0 第一次就被放行成 unavailable close（见 #112）。
    waits = int(queue.get("blind_review_artifact_waits", 0) or 0)
    # 文件从未存在 ≠ 盲审做不了，是盲审没发生。sentinel 实测 coordinator 可以 32 秒内
    # 连打两次把 wait 烧掉换 unavailable close；所以缺文件时永不降级——单元一直退回
    # pending，收不了场就由 supervisor 以非零码交人（#151）。
    if not report["present"] or stale:
        unit["status"] = "pending"
        unit["ended_at"] = None
        drop_claim(unit)
        queue["iteration"] = int(queue.get("iteration", 0) or 0) + 1
        write_queue(project_root, queue)
        append_decision(
            project_root,
            f"event=after_blind_review unit={unit_id} outcome=blind_review_pending_artifact "
            f"present={report['present']} n_reviews={report['n_reviews']} "
            f"note={'stale_report' if stale else 'absent_report_never_concedes'}",
        )
        print(json.dumps({"status": "pending", "outcome": "blind_review_pending_artifact",
                          "stale": stale, "blind_review": report,
                          "counts": queue_counts(queue)}, ensure_ascii=False))
        return

    unusable = report["n_reviews"] < MIN_BLIND_REVIEWS
    if unusable and waits < MAX_BLIND_REVIEW_ARTIFACT_WAITS:
        queue["blind_review_artifact_waits"] = waits + 1
        unit["status"] = "pending"
        unit["ended_at"] = None
        drop_claim(unit)
        queue["iteration"] = int(queue.get("iteration", 0) or 0) + 1
        write_queue(project_root, queue)
        append_decision(
            project_root,
            f"event=after_blind_review unit={unit_id} outcome=blind_review_pending_artifact "
            f"present={report['present']} n_reviews={report['n_reviews']} "
            f"waits={waits + 1}/{MAX_BLIND_REVIEW_ARTIFACT_WAITS}",
        )
        print(json.dumps({"status": "pending", "outcome": "blind_review_pending_artifact",
                          "waits": waits + 1, "blind_review": report,
                          "counts": queue_counts(queue)}, ensure_ascii=False))
        return

    unit["status"] = "done"
    unit["ended_at"] = now_iso()
    drop_claim(unit)
    rating = report["avg_rating"]
    weaknesses = report["top_weaknesses"]
    rounds = int(queue.get("blind_review_rounds", 0) or 0)
    current_cycle = int(queue.get("current_cycle", 0) or 0)
    max_cycles = int(queue.get("max_cycles", DEFAULT_MAX_CYCLES) or DEFAULT_MAX_CYCLES)
    unit["result"] = f"avg_rating={rating} decision={report['decision']} gap={report['calibration_gap']}"

    # 等完预算仍未凑齐两份独立评审时，单个模型的分数不能代表面板判断，也不能拿它开启
    # 修订轮。这种情况只能明确按 unavailable 收场，不能静默当作有效盲审。
    can_revise = (
        report["n_reviews"] >= MIN_BLIND_REVIEWS
        and rating is not None
        and rating < BLIND_REVIEW_RATING_THRESHOLD
        and weaknesses
        and rounds < MAX_BLIND_REVIEW_ROUNDS
        and current_cycle + 1 < max_cycles
    )

    if can_revise:
        queue["blind_review_rounds"] = rounds + 1
        next_cycle = current_cycle + 1
        existing = {u["id"] for u in queue.get("units", [])}
        new_units = [u for u in next_cycle_units(next_cycle, unit_id) if u["id"] not in existing]
        queue["units"].extend(new_units)
        queue["current_cycle"] = next_cycle
        queue.setdefault("cycle_status", {})[str(next_cycle)] = {
            "stage": "blind-review-revision",
            "status": "pending",
            "started_at": None,
            "ended_at": None,
            "derived_from": current_cycle,
            "focus": weaknesses[:3],
            "key_findings": [],
            "next_focus": [],
            "stop_reason": None,
        }
        outcome = f"blind_revision_cycle_{next_cycle}_appended:{len(new_units)} rating={rating}"
    else:
        if rating is not None and report["n_reviews"] >= MIN_BLIND_REVIEWS:
            reason = f"blind_review_rating_{rating}"
        elif report["n_reviews_declared"]:
            # reviewer 自己写下了零评审，这是它如实报告失败，等过一轮就认。
            reason = "blind_review_unavailable"
        else:
            # 一个合同字段都读不出来：可能真没评审，也可能评审了而字段换了写法。
            # 记成 unavailable 就等于替它认了下限，而那个 reason 是 close 那一步的
            # 免检牌，格式问题会被静默放行。这个 reason 不免检，收不了场交人（#241）。
            reason = "blind_review_unparsable"
        appended = append_close(queue, unit_id, reason)
        outcome = f"close_appended rating={rating}" if appended else "close_already_present"

    queue["iteration"] = int(queue.get("iteration", 0) or 0) + 1
    write_queue(project_root, queue)
    artifact = blind_review_path(project_root)
    record_terminal_unit(
        project_root,
        unit,
        "after-blind-review",
        {
            "artifact": str(artifact.relative_to(project_root)),
            "artifact_sha256": sha256_file(artifact),
            "artifact_mtime_ns": artifact.stat().st_mtime_ns,
            "n_reviews": report["n_reviews"],
            "avg_rating": rating,
            "decision": report["decision"],
        },
    )
    append_decision(
        project_root,
        f"event=after_blind_review unit={unit_id} outcome={outcome} "
        f"avg_rating={rating} calibration_gap={report['calibration_gap']} "
        f"rounds={queue.get('blind_review_rounds', rounds)}",
    )
    print(
        json.dumps(
            {"status": "ok", "outcome": outcome, "blind_review": report, "counts": queue_counts(queue)},
            ensure_ascii=False,
        )
    )


def command_claim(args: argparse.Namespace) -> None:
    """自组织抢占：worker 原子领取一个就绪单元并持有租约。

    整个 读队列→回收过期租约→挑单元→写租约→写回 在 flock 下完成，
    多 worker 并发 claim 不会拿到同一个单元。
    """
    project_root = Path(args.project_root).resolve()
    types = {t.strip() for t in (args.types or "").split(",") if t.strip()} or None
    with queue_lock(project_root):
        queue = load_queue(project_root)
        reclaimed = reclaim_expired_leases(queue, project_root)
        ready = claimable_units(queue, types)
        if not ready:
            write_queue(project_root, queue)
            counts = queue_counts(queue)
            active = counts["pending"] + counts["running"]
            payload = {
                "status": "empty",
                "reason": "queue_drained" if active == 0 else "all_ready_units_claimed_or_blocked",
                "reclaimed": reclaimed,
                "counts": counts,
            }
            print(json.dumps(payload, ensure_ascii=False))
            return
        unit = ready[0]
        now = datetime.now(timezone.utc)
        unit["status"] = "running"
        unit["claimed_by"] = args.worker
        unit["claimed_at"] = now_iso()
        # 每次领取都重置：单元被退回重来时保留旧时间戳，会让上一轮的产物看起来是这一轮写的。
        unit["started_at"] = now_iso()
        unit["claim_attempts"] = int(unit.get("claim_attempts", 0) or 0) + 1
        unit["lease_expires_at"] = datetime.fromtimestamp(
            now.timestamp() + args.lease_seconds, tz=timezone.utc
        ).isoformat(timespec="seconds")
        if unit.get("type") == "run":
            prepare_run_artifact_namespaces(project_root, queue)
        write_queue(project_root, queue)
        append_decision(
            project_root,
            f"event=unit_claimed unit={unit['id']} worker={args.worker} "
            f"lease={args.lease_seconds}s attempt={unit['claim_attempts']} ready_remaining={len(ready) - 1}",
        )
        counts = queue_counts(queue)
    if args.prompt:
        print(worker_unit_prompt(project_root, unit, args.worker, counts, len(ready) - 1))
    else:
        print(
            json.dumps(
                {
                    "status": "claimed",
                    "unit": unit,
                    "ready_remaining": len(ready) - 1,
                    "reclaimed": reclaimed,
                    "counts": counts,
                },
                ensure_ascii=False,
            )
        )


def command_ready(args: argparse.Namespace) -> None:
    """列出当前可抢占的就绪面（只回收过期租约，不抢占）。coordinator 用它决定铺几个 worker。"""
    project_root = Path(args.project_root).resolve()
    with queue_lock(project_root):
        queue = load_queue(project_root)
        reclaimed = reclaim_expired_leases(queue, project_root)
        ready = claimable_units(queue)
        write_queue(project_root, queue)
    print(
        json.dumps(
            {
                "status": "ok",
                "ready": [{"id": u["id"], "type": u.get("type"), "cycle": u.get("cycle")} for u in ready],
                "width": len(ready),
                "reclaimed": reclaimed,
                "counts": queue_counts(queue),
            },
            ensure_ascii=False,
        )
    )


def command_heartbeat(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    with queue_lock(project_root):
        queue = load_queue(project_root)
        units = unit_by_id(queue)
        if args.unit not in units:
            raise SystemExit(f"unknown unit: {args.unit}")
        unit = units[args.unit]
        if unit.get("claimed_by") != args.worker:
            print(
                json.dumps(
                    {"status": "rejected", "reason": "claim_lost", "current_holder": unit.get("claimed_by")},
                    ensure_ascii=False,
                )
            )
            raise SystemExit(3)
        unit["lease_expires_at"] = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + args.lease_seconds, tz=timezone.utc
        ).isoformat(timespec="seconds")
        write_queue(project_root, queue)
    print(json.dumps({"status": "ok", "lease_expires_at": unit["lease_expires_at"]}, ensure_ascii=False))


def command_release(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    with queue_lock(project_root):
        queue = load_queue(project_root)
        units = unit_by_id(queue)
        if args.unit not in units:
            raise SystemExit(f"unknown unit: {args.unit}")
        unit = units[args.unit]
        if unit.get("claimed_by") != args.worker:
            print(json.dumps({"status": "rejected", "reason": "claim_lost"}, ensure_ascii=False))
            raise SystemExit(3)
        unit["status"] = "pending"
        unit.pop("claimed_by", None)
        unit.pop("lease_expires_at", None)
        write_queue(project_root, queue)
        append_decision(project_root, f"event=unit_released unit={args.unit} worker={args.worker}")
    print(json.dumps({"status": "ok", "counts": queue_counts(queue)}, ensure_ascii=False))


def blind_review_gaps(project_root: Path, queue: dict[str, Any]) -> list[str]:
    """close 之前，盲审这一关还差什么。

    判据是产物，不是单元状态：把单元标成 done 是模型能做到的事，写出一份带 n_reviews
    的报告不是。两者分开，门才拦得住「召唤了但没等」。

    `blind_review_unavailable` 是显式记录过的降级，不在这里重判——它已经在
    after-blind-review 里等过一轮了。`blind_review_unparsable` 不在此列：那说明
    产物里一个合同字段都读不出来，评审到底做没做成还不知道，得有人看一眼。
    """
    gaps = []
    units = [u for u in queue.get("units", []) if u.get("type") == "blind-review"]
    if not units:
        return gaps
    report = read_blind_review_report(project_root)
    # 降级的前提是「真实尝试过」：文件在、只是零评审。文件不在的 unavailable 是
    # 伪造或手改出来的（引擎在缺文件时根本不会追加这种 close），要重判。
    conceded = report["present"] and any(
        "blind_review_unavailable" in str(u.get("reason", ""))
        for u in queue.get("units", []) if u.get("type") == "close")
    if conceded:
        return gaps
    if not report["present"]:
        gaps.append("blind_review.md 不存在")
    elif report["n_reviews"] < MIN_BLIND_REVIEWS:
        gaps.append(
            f"blind_review.md 里 n_reviews={report['n_reviews']}，"
            f"需要至少 {MIN_BLIND_REVIEWS} 份独立评审"
        )
    return gaps


def skip_provenance_gaps(project_root: Path, queue: dict[str, Any]) -> list[str]:
    """Verify skipped revision units against the engine mirror and structured record."""
    skipped = [
        unit for unit in queue.get("units", [])
        if unit.get("status") == "skipped"
        and (
            unit.get("type") in ADJUDICATION
            or unit.get("type") == "run"
            or int(unit.get("cycle", 0) or 0) >= 1
        )
    ]
    if not skipped:
        return []

    records = queue.get("cycle_skip_records")
    records = records if isinstance(records, dict) else {}
    mirror = read_json(mirror_path(project_root))
    mirror_records = mirror.get("cycle_skip_records")
    mirror_records = mirror_records if isinstance(mirror_records, dict) else {}
    units = unit_by_id(queue)
    mirror_units = unit_by_id(mirror)
    cycle_status = queue.get("cycle_status")
    cycle_status = cycle_status if isinstance(cycle_status, dict) else {}
    gaps: list[str] = []

    for cycle in sorted({int(unit.get("cycle", 0) or 0) for unit in skipped}):
        key = str(cycle)
        record = records.get(key)
        mirror_record = mirror_records.get(key)
        if not isinstance(record, dict):
            gaps.append(f"cycle {cycle} 的 skipped 单元没有结构化 skip record")
            continue
        if record != mirror_record:
            gaps.append(f"cycle {cycle} 的 skip record 与 engine mirror 不一致")
            continue

        analysis = record.get("analysis")
        critic = record.get("critic")
        listed = record.get("units")
        reason = record.get("reason")
        cycle_entry = cycle_status.get(key)
        if record.get("cycle") != cycle or not isinstance(listed, list) or not reason:
            gaps.append(f"cycle {cycle} 的 skip record 字段不完整")
            continue
        if not isinstance(analysis, dict) or analysis.get("cycle") != cycle or analysis.get("decision") != "stop":
            gaps.append(f"cycle {cycle} 的 analysis skip provenance 无效")
        if not isinstance(critic, dict) or critic.get("cycle") != cycle or critic.get("verdict") not in {
            "finish_ok", "approve"
        }:
            gaps.append(f"cycle {cycle} 的 critic skip provenance 无效")
        if not isinstance(cycle_entry, dict) or cycle_entry.get("status") != "skipped":
            gaps.append(f"cycle {cycle} 的 cycle_status 未记录 skipped")
        elif (
            cycle_entry.get("analysis_decision") != analysis
            or cycle_entry.get("critic_decision") != critic
            or cycle_entry.get("skip_record") != record
        ):
            gaps.append(f"cycle {cycle} 的 cycle_status 与 skip record 不一致")

        listed_ids = {str(unit_id) for unit_id in listed}
        for unit in skipped:
            if int(unit.get("cycle", 0) or 0) == cycle and unit["id"] not in listed_ids:
                gaps.append(f"skipped 单元 {unit['id']} 不在 cycle {cycle} 的 skip record 中")
        for unit_id in listed_ids:
            unit = units.get(unit_id)
            mirrored = mirror_units.get(unit_id)
            if unit is None or unit.get("status") != "skipped" or unit.get("reason") != reason:
                gaps.append(f"skip record 中的单元 {unit_id} 未保持对应 skipped 状态")
                continue
            if mirrored is None or any(
                mirrored.get(field) != unit.get(field) for field in ("cycle", "status", "reason", "ended_at")
            ):
                gaps.append(f"skipped 单元 {unit_id} 与 engine mirror 不一致")
    return gaps


def terminal_event_gaps(project_root: Path, queue: dict[str, Any]) -> list[str]:
    """Match every terminal queue unit to the engine's append-only hash chain."""
    authority_enabled = (
        queue.get("completion_authority_version") == COMPLETION_AUTHORITY_VERSION
        or engine_events_path(project_root).exists()
    )
    if not authority_enabled:
        # Legacy handcrafted fixtures and pre-cutover projects have no authority ledger.
        # A fresh init always enables it; there is no silent downgrade once the file exists.
        return []
    events, problems = read_engine_events(project_root)
    gaps = list(problems)
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") == "unit_terminal" and event.get("unit"):
            latest[str(event["unit"])] = event
    for unit in queue.get("units", []):
        if unit.get("status") not in TERMINAL_STATUSES:
            continue
        unit_id = str(unit.get("id") or "")
        event = latest.get(unit_id)
        if event is None:
            gaps.append(f"terminal unit {unit_id} 没有 engine event")
            continue
        expected = {
            "unit_type": str(unit.get("type") or ""),
            "cycle": int(unit.get("cycle", 0) or 0),
            "status": unit.get("status"),
            "ended_at": unit.get("ended_at"),
            "reason": unit.get("reason"),
        }
        mismatched = [field for field, value in expected.items() if event.get(field) != value]
        if mismatched:
            gaps.append(
                f"terminal unit {unit_id} 与 engine event 不一致：{', '.join(mismatched)}"
            )
    return gaps


def review_terminal_evidence(
    project_root: Path,
    unit: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Recheck the mutable review against the engine event that admitted it."""
    events, problems = read_engine_events(project_root)
    gaps = list(problems)
    event = next(
        (
            item
            for item in reversed(events)
            if item.get("kind") == "unit_terminal"
            and item.get("unit") == unit.get("id")
            and item.get("unit_type") == "review"
        ),
        None,
    )
    if event is None:
        return [*gaps, f"review unit {unit.get('id')} 没有 terminal event"], {}

    current_gaps, current = review_evidence(project_root, unit)
    gaps.extend(current_gaps)
    receipt_gaps, producer_receipt = review_receipt_evidence(
        project_root,
        unit,
        project_root / "review.md",
        read_review_report(project_root),
    )
    gaps.extend(receipt_gaps)
    recorded = event.get("evidence")
    recorded = recorded if isinstance(recorded, dict) else {}
    if recorded.get("artifact") != "review.md" or not recorded.get("artifact_sha256"):
        gaps.append(f"review unit {unit.get('id')} 的 terminal event 没有 artifact hash")
    elif current.get("artifact_sha256") != recorded.get("artifact_sha256"):
        gaps.append(f"review unit {unit.get('id')} 的 review.md 在 terminal event 后发生漂移")
    for field in ("model", "model_identity", "reviewer"):
        if current.get(field) != recorded.get(field):
            gaps.append(f"review unit {unit.get('id')} 的 {field} 与 terminal event 不一致")
    recorded_receipt = recorded.get("producer_receipt")
    recorded_receipt = recorded_receipt if isinstance(recorded_receipt, dict) else {}
    for field in (
        "event_hash",
        "request_id",
        "artifact_sha256",
        "reviewer",
        "model",
        "model_identity",
        "blockers_count",
    ):
        if recorded_receipt.get(field) != producer_receipt.get(field):
            gaps.append(
                f"review unit {unit.get('id')} 的 {field} 与 producer receipt 不一致"
            )
    return gaps, {
        "unit": unit.get("id"),
        "event_hash": event.get("event_hash"),
        "artifact": recorded.get("artifact"),
        "artifact_sha256": recorded.get("artifact_sha256"),
        "producer_receipt": producer_receipt,
    }


def direct_review_predecessors(queue: dict[str, Any], unit: dict[str, Any]) -> list[dict[str, Any]]:
    blocked_by = unit.get("blocked_by")
    blocker_ids = blocked_by if isinstance(blocked_by, list) else [blocked_by]
    units = unit_by_id(queue)
    return [
        predecessor
        for blocker in blocker_ids
        if blocker in units
        for predecessor in [units[str(blocker)]]
        if predecessor.get("type") == "review" and predecessor.get("status") == "done"
    ]


def latest_review_terminal_gaps(project_root: Path, queue: dict[str, Any]) -> list[str]:
    reviews = [
        unit
        for unit in queue.get("units", [])
        if unit.get("type") == "review" and unit.get("status") == "done"
    ]
    if not reviews:
        return []
    events, event_problems = read_engine_events(project_root)
    gaps = list(event_problems)
    receipt_fields = (
        "event_hash",
        "request_id",
        "artifact_sha256",
        "reviewer",
        "model",
        "model_identity",
        "blockers_count",
    )
    for unit in reviews:
        receipt_gaps, current = review_receipt_evidence(project_root, unit)
        gaps.extend(f"review unit {unit.get('id')}: {gap}" for gap in receipt_gaps)
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.get("kind") == "unit_terminal"
                and event.get("unit") == unit.get("id")
                and event.get("source") == "complete"
            ),
            None,
        )
        if terminal is None:
            gaps.append(f"review unit {unit.get('id')} 没有 complete terminal event")
            continue
        evidence = terminal.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        recorded = evidence.get("producer_receipt")
        recorded = recorded if isinstance(recorded, dict) else {}
        for field in receipt_fields:
            if recorded.get(field) != current.get(field):
                gaps.append(
                    f"review unit {unit.get('id')} 的 {field} 与 producer receipt 不一致"
                )
    live_gaps, _ = review_terminal_evidence(project_root, reviews[-1])
    gaps.extend(live_gaps)
    return gaps


def terminal_run_receipt_gaps(project_root: Path, queue: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    for unit in queue.get("units", []):
        if unit.get("type") != "run" or unit.get("status") != "done":
            continue
        unit_gaps, _ = run_receipt_evidence(
            project_root,
            unit,
            queue,
        )
        gaps.extend(f"{unit.get('id')}: {gap}" for gap in unit_gaps)
    return gaps


def terminal_critic_receipt_gaps(project_root: Path, queue: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    events, event_problems = read_engine_events(project_root)
    gaps.extend(event_problems)
    critics = [
        unit
        for unit in queue.get("units", [])
        if unit.get("type") == "critic" and unit.get("status") == "done"
    ]
    for index, unit in enumerate(critics):
        receipt_event = next(
            (
                event
                for event in reversed(events)
                if event.get("kind") == "critic_receipt"
                and event.get("unit") == unit.get("id")
                and event.get("cycle") == int(unit.get("cycle", 0) or 0)
            ),
            None,
        )
        if receipt_event is None:
            gaps.append(f"critic unit {unit.get('id')} 没有 producer receipt event")
            continue
        payload = receipt_event.get("receipt")
        payload = payload if isinstance(payload, dict) else {}
        payload_critics = (
            payload.get("critics") if isinstance(payload.get("critics"), list) else []
        )
        current = {
            "event_hash": receipt_event.get("event_hash"),
            "request_id": payload.get("request_id"),
            "artifact_sha256": payload.get("artifact_sha256"),
            "verdict": payload.get("verdict"),
            "model_identities": [
                item.get("model_identity")
                for item in payload_critics
                if isinstance(item, dict) and item.get("status") == "ok"
            ],
            "response_sha256": [
                item.get("response_sha256")
                for item in payload_critics
                if isinstance(item, dict) and item.get("status") == "ok"
            ],
        }
        if receipt_event.get("source") != CRITIC_RECEIPT_SOURCE:
            gaps.append(f"critic unit {unit.get('id')} 的 receipt source 无效")
        if payload.get("unit") != unit.get("id"):
            gaps.append(f"critic unit {unit.get('id')} 的 receipt unit 不一致")
        if payload.get("cycle") != int(unit.get("cycle", 0) or 0):
            gaps.append(f"critic unit {unit.get('id')} 的 receipt cycle 不一致")
        payload_gaps = critic_receipt_payload_gaps(payload, unit)
        gaps.extend(f"critic unit {unit.get('id')}: {gap}" for gap in payload_gaps)
        # critic.md is intentionally overwritten by later cycles. The ledger preserves
        # historical bytes; only the latest critic can be rechecked against the live file.
        if index == len(critics) - 1:
            report = read_critic_report(project_root)
            artifact = critic_path(project_root)
            unit_gaps, live = critic_receipt_evidence(
                project_root,
                unit,
                artifact,
                report,
            )
            gaps.extend(f"{unit.get('id')}: {gap}" for gap in unit_gaps)
            current = live
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.get("kind") == "unit_terminal"
                and event.get("unit") == unit.get("id")
                and event.get("source") == "after-critic"
            ),
            None,
        )
        if terminal is None:
            gaps.append(f"critic unit {unit.get('id')} 没有 after-critic terminal event")
            continue
        evidence = terminal.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        recorded = evidence.get("producer_receipt")
        recorded = recorded if isinstance(recorded, dict) else {}
        for field in (
            "event_hash",
            "request_id",
            "artifact_sha256",
            "verdict",
            "model_identities",
            "response_sha256",
        ):
            if recorded.get(field) != current.get(field):
                gaps.append(
                    f"critic unit {unit.get('id')} 的 {field} 与 producer receipt 不一致"
                )
    return gaps


def command_complete(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    with queue_lock(project_root):
        queue = load_queue(project_root)
        units = unit_by_id(queue)
        if args.unit not in units:
            raise SystemExit(f"unknown unit: {args.unit}")
        unit = units[args.unit]
        reject_if_claim_lost(unit, args.worker)
        if args.status == "failed":
            # A coordinator used failed as a terminal shortcut after review evidence was rejected.
            # Keeping the unit running lets the supervisor's next session resume the same work;
            # a definitive human stop is represented by blocked and never satisfies dependencies.
            print(json.dumps({
                "status": "rejected",
                "reason": "required_unit_failure_is_retryable",
                "unit": unit["id"],
                "recovery": "保留当前 running 单元并停止本次会话；supervisor 会从同一单元恢复。"
                            "确认需要人工介入时使用 --status blocked。",
            }, ensure_ascii=False))
            raise SystemExit(6)
        # 这三型单元的「完成」就是裁决本身：分析要拉起 critic 链，critic 要决定下一轮，
        # 盲审要按分数裁 close 还是修订。complete 只写状态，于是队列排空却永远差一个
        # close（官方 run 2 的 result_analysis_c1 就这样被批掉）。判据只有 ADJUDICATION
        # 一处，门和两种提示读的是同一张表（#218）。
        adjudication = adjudication_command(project_root, unit, args.worker)
        if adjudication and args.status in {"done", "skipped"}:
            # skipped 同样是「不会再做」的终态声明。E2E 负控实测：#222 只拦 done，
            # coordinator 换成 --status skipped 三连 rc=0 直通 verify-close（#253）。
            print(json.dumps({"status": "rejected", "reason": "adjudication_required",
                              "unit": unit["id"], "type": unit.get("type"),
                              "command": adjudication}, ensure_ascii=False))
            raise SystemExit(6)

        if args.status == "skipped" and unit.get("type") == "review":
            print(json.dumps({
                "status": "rejected",
                "reason": "review_skip_requires_evidence",
                "unit": unit["id"],
                "command": f"{ENGINE_COMMAND} complete --project-root {project_root} "
                           f"--unit {unit['id']} --status done",
            }, ensure_ascii=False))
            raise SystemExit(6)

        if args.status == "skipped" and unit.get("type") == "run":
            cycle = int(unit.get("cycle", 0) or 0)
            command = (
                f"{ENGINE_COMMAND} skip-cycle --project-root {project_root} --cycle {cycle}"
                if cycle >= 1
                else f"{ENGINE_COMMAND} complete --project-root {project_root} "
                f"--unit {unit['id']} --status done"
            )
            print(json.dumps({
                "status": "rejected",
                "reason": "run_skip_requires_adjudication",
                "unit": unit["id"],
                "cycle": cycle,
                "command": command,
            }, ensure_ascii=False))
            raise SystemExit(6)

        if args.status == "skipped" and int(unit.get("cycle", 0) or 0) >= 1:
            # 修订链是裁决排进来的，清掉它同样要过裁决：单跳会把「这条链还需不需要」
            # 的判断散落在 N 次没有判据的 complete 里。整链跳只有 skip-cycle 一个入口，
            # It verifies the same-cycle engine analysis decision and corresponding critic first.
            print(json.dumps({
                "status": "rejected", "reason": "cycle_skip_requires_adjudication",
                "unit": unit["id"], "cycle": unit.get("cycle"),
                "command": f"{ENGINE_COMMAND} skip-cycle --project-root {project_root} "
                           f"--cycle {unit.get('cycle')}"}, ensure_ascii=False))
            raise SystemExit(6)

        completion_evidence: dict[str, Any] = {}
        evidence_gaps: list[str] = []
        if args.status == "done" and unit.get("type") == "run":
            evidence_gaps, completion_evidence = run_receipt_evidence(
                project_root,
                unit,
                queue,
            )
            review_prerequisites = []
            for review_unit in direct_review_predecessors(queue, unit):
                review_gaps, review_event = review_terminal_evidence(
                    project_root,
                    review_unit,
                )
                evidence_gaps.extend(
                    f"review prerequisite {review_unit['id']}: {gap}" for gap in review_gaps
                )
                review_prerequisites.append(review_event)
            if review_prerequisites:
                completion_evidence["review_prerequisites"] = review_prerequisites
        elif args.status == "done" and unit.get("type") == "review":
            evidence_gaps, completion_evidence = review_evidence(project_root, unit)
            receipt_gaps, producer_receipt = review_receipt_evidence(
                project_root,
                unit,
                project_root / "review.md",
                read_review_report(project_root),
            )
            evidence_gaps.extend(receipt_gaps)
            completion_evidence["producer_receipt"] = producer_receipt
        if evidence_gaps:
            print(json.dumps({
                "status": "rejected",
                "reason": "completion_evidence_incomplete",
                "unit": unit["id"],
                "missing": evidence_gaps,
            }, ensure_ascii=False))
            raise SystemExit(4)

        if unit.get("type") == "close" and args.status == "done":
            # sentinel 实测：close 落 done 时六个单元还 pending。close 是队列的句号，
            # 别的单元还活着就不许画（#151）。
            still_active = [
                u["id"]
                for u in queue.get("units", [])
                if u["id"] != unit["id"] and u.get("status") not in TERMINAL_STATUSES
            ]
            if still_active:
                print(json.dumps({"status": "rejected", "reason": "queue_still_active",
                                  "active": still_active}, ensure_ascii=False))
                raise SystemExit(4)
            # close 不可逆：它一落地 coordinator 就输出 AUTORESEARCH_DONE。所以这里再核一次
            # 盲审真的做过——不是「有个单元被标了 done」，是产物在、且达到双评审门槛。实测过
            # 一次 close 发生在盲审文件落盘之前五分钟。
            missing = [
                *failed_unit_gaps(queue),
                *skip_provenance_gaps(project_root, queue),
                *blind_review_gaps(project_root, queue),
                *terminal_event_gaps(project_root, queue),
                *latest_review_terminal_gaps(project_root, queue),
                *terminal_run_receipt_gaps(project_root, queue),
                *terminal_critic_receipt_gaps(project_root, queue),
                *project_permission_gaps(project_root),
            ]
            if missing:
                print(json.dumps({"status": "rejected", "reason": "completion_evidence_incomplete",
                                  "missing": missing}, ensure_ascii=False))
                raise SystemExit(4)
        unit["status"] = args.status
        unit["ended_at"] = now_iso()
        unit.pop("lease_expires_at", None)
        if args.result:
            unit["result"] = args.result
        queue["iteration"] = int(queue.get("iteration", 0) or 0) + 1
        write_queue(project_root, queue)
        record_terminal_unit(
            project_root,
            unit,
            "complete",
            completion_evidence,
        )
        append_decision(
            project_root,
            f"event=unit_marked unit={args.unit} status={args.status}"
            + (f" worker={args.worker}" if args.worker else ""),
        )
    print(json.dumps({"status": "ok", "counts": queue_counts(queue)}, ensure_ascii=False))


def command_skip_cycle(args: argparse.Namespace) -> None:
    """Moot a revision cycle using same-cycle analysis and critic evidence.

    The engine records the decision in a structured cycle record and mirrors it atomically.
    Per-unit reason text is descriptive and never serves as completion authority.
    """
    project_root = Path(args.project_root).resolve()
    cycle = int(args.cycle)
    if cycle < 1:
        raise SystemExit("skip-cycle 只作用于修订链（cycle >= 1）；cycle 0 是主线，不能跳")
    with queue_lock(project_root):
        queue = load_queue(project_root)
        targets = [u for u in queue.get("units", [])
                   if int(u.get("cycle", 0) or 0) == cycle
                   and u.get("status") in ACTIVE_STATUSES
                   and u.get("type") not in {"close", "blind-review"}]
        if not targets:
            print(json.dumps({"status": "rejected", "reason": "no_active_units_in_cycle",
                              "cycle": cycle}, ensure_ascii=False))
            raise SystemExit(5)

        units = unit_by_id(queue)
        analysis = queue.get("last_analysis_decision") or {}
        mirror_analysis = read_json(mirror_path(project_root)).get("last_analysis_decision")
        analysis_unit = units.get(str(analysis.get("unit") or ""))
        expected_critic_id = critic_unit_id(str(analysis.get("unit") or ""))
        critic_unit = units.get(expected_critic_id)
        critic = read_critic_report(project_root)
        critic_file = adjudication_artifact_path(project_root, "critic")
        critic_fresh = bool(
            critic_unit
            and critic_unit.get("started_at")
            and critic_unit.get("status") in {"running", "done"}
            and critic_file.exists()
            and artifact_belongs_to(critic_file, critic_unit)
        )
        verdict = str(critic.get("verdict") or "").strip().lower()
        critic_receipt_gaps, critic_receipt = (
            critic_receipt_evidence(project_root, critic_unit, critic_file, critic)
            if critic_unit and critic_file.exists()
            else (["critic producer receipt 缺失"], {})
        )

        holds = []
        if analysis.get("decision") != "stop":
            holds.append(
                "没有引擎落账的分析停止裁决（after-result-analysis --decision stop）；"
                f"当前记录：{analysis or '(无)'}")
        if analysis != mirror_analysis:
            holds.append("分析停止裁决与 engine mirror 不一致，不是当前引擎落账的记录")
        if analysis.get("cycle") != cycle:
            holds.append(f"分析裁决属于 cycle {analysis.get('cycle')}，不是目标 cycle {cycle}")
        if (
            not analysis_unit
            or analysis_unit.get("type") != "result-analysis"
            or int(analysis_unit.get("cycle", 0) or 0) != cycle
            or analysis_unit.get("status") != "done"
        ):
            holds.append("分析裁决没有绑定目标 cycle 中已完成的 result-analysis 单元")
        if (
            analysis.get("artifact") != ADJUDICATION["result-analysis"]["artifact"]
            or not isinstance(analysis.get("artifact_mtime_ns"), int)
        ):
            holds.append("分析裁决没有绑定引擎核验过的结构化分析产物")
        if (
            not critic_unit
            or critic_unit.get("type") != "critic"
            or int(critic_unit.get("cycle", 0) or 0) != cycle
            or critic_unit.get("blocked_by") != analysis.get("unit")
        ):
            holds.append("critic 单元不是目标 cycle 中对应 result-analysis 的 critic")
        if not critic_fresh:
            holds.append("critic.md 缺失或属于上一轮（先产出本轮 critic 产物）")
        elif verdict not in {"finish_ok", "approve"}:
            holds.append(f"本轮 critic verdict 是 {verdict or '(空)'}，不是 finish_ok")
        holds.extend(critic_receipt_gaps)
        if holds:
            print(json.dumps({"status": "rejected", "reason": "adjudication_not_met",
                              "holds": holds, "cycle": cycle}, ensure_ascii=False))
            raise SystemExit(5)

        provenance = (f"cycle_mooted:analysis_stop={analysis.get('unit')}"
                      f"@cycle{analysis.get('cycle')};critic={verdict}")
        decided_at = now_iso()
        critic_decision = {
            "cycle": cycle,
            "unit": expected_critic_id,
            "verdict": verdict,
            "artifact": str(ADJUDICATION["critic"]["artifact"]),
            "artifact_mtime_ns": critic_file.stat().st_mtime_ns,
            "artifact_sha256": sha256_file(critic_file),
            "producer_receipt": critic_receipt,
        }
        record = {
            "schema_version": 1,
            "cycle": cycle,
            "units": [unit["id"] for unit in targets],
            "analysis": analysis,
            "critic": critic_decision,
            "reason": provenance,
            "at": decided_at,
        }
        for unit in targets:
            unit["status"] = "skipped"
            unit["ended_at"] = decided_at
            unit["reason"] = provenance
            drop_claim(unit)
        queue.setdefault("cycle_skip_records", {})[str(cycle)] = record
        cycle_status = queue.setdefault("cycle_status", {})
        entry = cycle_status.setdefault(str(cycle), {})
        entry["status"] = "skipped"
        entry["ended_at"] = decided_at
        entry["analysis_decision"] = analysis
        entry["critic_decision"] = critic_decision
        entry["skip_record"] = record
        queue["iteration"] = int(queue.get("iteration", 0) or 0) + 1
        write_queue(project_root, queue)
        for unit in targets:
            record_terminal_unit(
                project_root,
                unit,
                "skip-cycle",
                {"skip_record": record},
            )
        append_decision(
            project_root,
            f"event=skip_cycle cycle={cycle} units={','.join(u['id'] for u in targets)} "
            f"analysis_stop={analysis.get('unit')}@cycle{analysis.get('cycle')} "
            f"critic_verdict={verdict}",
        )
    print(json.dumps({"status": "ok", "skipped": [u["id"] for u in targets],
                      "counts": queue_counts(queue)}, ensure_ascii=False))


def command_verify_close(args: argparse.Namespace) -> None:
    """只读复核：这个项目算不算真的收尾了。

    supervisor 此前直接解析 workflow_queue.json，而队列文件 coordinator 写得到——
    手改一个 status=done 就能让它 exit 0。把完成判定收回引擎：close 单元存在且 done、
    队列没有 active 单元、盲审产物齐、skip provenance 与 mirror 一致才 exit 0。
    """
    project_root = Path(args.project_root).resolve()
    queue = load_queue(project_root)
    problems = []
    close = next((u for u in queue.get("units", []) if u.get("type") == "close"), None)
    if close is None or close.get("status") != "done":
        problems.append("close 单元不存在或未完成")
    still_active = [
        u["id"]
        for u in queue.get("units", [])
        if u.get("type") != "close" and u.get("status") not in TERMINAL_STATUSES
    ]
    if still_active:
        problems.append(f"队列仍有 active 单元：{', '.join(still_active)}")
    problems.extend(failed_unit_gaps(queue))
    # A reason prefix is coordinator-writable text. Engine-sanctioned skips carry a
    # structured record that must still match the engine mirror and every skipped unit.
    problems.extend(skip_provenance_gaps(project_root, queue))
    problems.extend(blind_review_gaps(project_root, queue))
    problems.extend(terminal_event_gaps(project_root, queue))
    problems.extend(latest_review_terminal_gaps(project_root, queue))
    problems.extend(terminal_run_receipt_gaps(project_root, queue))
    problems.extend(terminal_critic_receipt_gaps(project_root, queue))
    problems.extend(project_permission_gaps(project_root))
    verdict = {"status": "done" if not problems else "not_done", "problems": problems}
    print(json.dumps(verdict, ensure_ascii=False))
    raise SystemExit(0 if not problems else 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AutoResearch workflow queue engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_close = subparsers.add_parser(
        "verify-close", help="Read-only: exit 0 only when the run is genuinely closed")
    verify_close.add_argument("--project-root", required=True)
    verify_close.set_defaults(func=command_verify_close)

    init = subparsers.add_parser("init", help="Initialize or normalize workflow_queue.json")
    init.add_argument("--project-root", required=True)
    init.add_argument("--max-cycles", type=int, default=None)
    init.set_defaults(func=command_init)

    next_prompt = subparsers.add_parser("next-prompt", help="Print the next coordinator prompt")
    next_prompt.add_argument("--project-root", required=True)
    next_prompt.set_defaults(func=command_next_prompt)

    # 这三条是那三型单元唯一的回写入口（#218），所以 complete 的所有权校验得跟着搬过来：
    # --worker 可选，抢占模式下的 worker 必须带，租约被回收后迟到的回写照样拒。
    ownership = "Claim-mode ownership check; rejected if lease was reclaimed"

    after = subparsers.add_parser("after-result-analysis", help="Append critic after result analysis")
    after.add_argument("--project-root", required=True)
    after.add_argument("--unit", required=True)
    after.add_argument("--worker", default="", help=ownership)
    after.add_argument("--decision", choices=["continue", "stop", "blocked"], default="continue",
                       help="Structured verdict of this analysis; skip-cycle only trusts an engine-recorded stop")
    after.set_defaults(func=command_after_result_analysis)

    after_critic = subparsers.add_parser("after-critic", help="Append next cycle or close after external critic")
    after_critic.add_argument("--project-root", required=True)
    after_critic.add_argument("--unit", required=True)
    after_critic.add_argument("--worker", default="", help=ownership)
    after_critic.set_defaults(func=command_after_critic)

    receipt = subparsers.add_parser(
        "record-critic-receipt",
        help=argparse.SUPPRESS,
    )
    receipt.add_argument("--project-root", required=True)
    receipt.set_defaults(func=command_record_critic_receipt)

    review_report = subparsers.add_parser(
        "record-review-report",
        help=argparse.SUPPRESS,
    )
    review_report.add_argument("--project-root", required=True)
    review_report.add_argument("--unit", required=True)
    review_report.add_argument("--cycle", required=True, type=int)
    review_report.set_defaults(func=command_record_review_report)

    after_blind = subparsers.add_parser(
        "after-blind-review", help="Decide close vs one revision cycle after blind review"
    )
    after_blind.add_argument("--project-root", required=True)
    after_blind.add_argument("--unit", required=True)
    after_blind.add_argument("--worker", default="", help=ownership)
    after_blind.set_defaults(func=command_after_blind_review)

    execute_run = subparsers.add_parser(
        "execute-run",
        help="Execute one stage-scoped experiment command and record engine provenance",
    )
    execute_run.add_argument("--project-root", required=True)
    execute_run.add_argument("--unit", required=True)
    execute_run.add_argument("--worker", default="", help=ownership)
    execute_run.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        help="Command after --; must use the project venv and bind stage/artifact/run-log",
    )
    execute_run.set_defaults(func=command_execute_run)

    complete = subparsers.add_parser("complete", help="Mark a unit terminal")
    complete.add_argument("--project-root", required=True)
    complete.add_argument("--unit", required=True)
    complete.add_argument("--status", choices=sorted(TERMINAL_STATUSES | {"blocked"}), default="done")
    complete.add_argument("--result", default="")
    complete.add_argument("--worker", default="", help="Claim-mode ownership check; rejected if lease was reclaimed")
    complete.set_defaults(func=command_complete)

    skip_cycle = subparsers.add_parser(
        "skip-cycle",
        help="Skip a revision cycle using its engine-recorded analysis stop and fresh corresponding critic")
    skip_cycle.add_argument("--project-root", required=True)
    skip_cycle.add_argument("--cycle", required=True, type=int)
    skip_cycle.add_argument("--worker", default="", help="Recorded in the decision log only")
    skip_cycle.set_defaults(func=command_skip_cycle)

    claim = subparsers.add_parser("claim", help="Self-organizing: atomically claim one ready unit with a lease")
    claim.add_argument("--project-root", required=True)
    claim.add_argument("--worker", required=True)
    claim.add_argument("--types", default="", help="Comma-separated unit types this worker accepts (default: any)")
    claim.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    claim.add_argument("--prompt", action="store_true", help="Print an executable worker prompt instead of JSON")
    claim.set_defaults(func=command_claim)

    ready = subparsers.add_parser("ready", help="List claimable units (reclaims expired leases)")
    ready.add_argument("--project-root", required=True)
    ready.set_defaults(func=command_ready)

    heartbeat = subparsers.add_parser("heartbeat", help="Extend the lease on a claimed unit")
    heartbeat.add_argument("--project-root", required=True)
    heartbeat.add_argument("--unit", required=True)
    heartbeat.add_argument("--worker", required=True)
    heartbeat.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    heartbeat.set_defaults(func=command_heartbeat)

    release = subparsers.add_parser("release", help="Give a claimed unit back to the pool")
    release.add_argument("--project-root", required=True)
    release.add_argument("--unit", required=True)
    release.add_argument("--worker", required=True)
    release.set_defaults(func=command_release)
    return parser


# 这些旧命令自身不加锁,由 main 统一套 flock;新命令(claim/ready/heartbeat/release/complete)内部自锁。
LOCKED_AT_MAIN = {
    "init",
    "next-prompt",
    "after-result-analysis",
    "after-critic",
    "after-blind-review",
    "record-critic-receipt",
    "record-review-report",
}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in LOCKED_AT_MAIN:
        with queue_lock(Path(args.project_root).resolve()):
            args.func(args)
    else:
        args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
