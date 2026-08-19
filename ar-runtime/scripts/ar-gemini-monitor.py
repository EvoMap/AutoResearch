#!/usr/bin/env python3
"""
AutoResearch Gemini Monitor

Plain Python daemon (NOT a subagent). Watches a result/log file produced
by ar-runner. When the file grows or gains new milestones, calls Gemini
API directly to summarize what just happened, appends summary to a
notifications log.

Usage:
    python ar-gemini-monitor.py --watch <file> [--notify-log <file>]
                                [--project-root <dir>] [--state <file>]
                                [--summary <file>] [--interval 30] [--once]

Model:
    摘要走流水线同一套角色解析，模型由 AR_MODEL_RUN_MONITOR 决定；不配就用配置里
    run_monitor 的推荐值。凭据是那个模型所在端点的凭据，跟别的角色一样。

    拿不到摘要时 monitor 仍然工作：心跳、idle/stale、summary_detected 都不依赖模型，
    notifications.log 只是少一段话。

Designed to be launched in background by ar-coordinator at the beginning
of the run via `Bash run_in_background:true ... &`. Coordinator records
the task_id and stops it at end of pipeline. The monitor is intentionally
run-level, not experiment-level: it starts before runner work exists,
keeps a heartbeat state file, reports log growth, reports idle/stale
periods, and emits a completion note once results/summary.md appears.

Stops gracefully on SIGTERM/SIGINT. Log every action to stderr so the
parent shell captures it.
"""
import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# ---- config ---------------------------------------------------------------

# 摘要走流水线同一套角色解析：换模型的旋钮（AR_MODEL_RUN_MONITOR）、preflight 报的模型、
# 真正发出去的模型，从此是同一个。原来这里只有 Vertex 一条路，缺凭据就静默返回空串，
# 于是 monitor 降级成只有心跳没有摘要，而 Vertex 已经退役（#73）。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# Trigger heuristics — file changes are noticed only when ONE of these is true:
MIN_BYTES_GROWTH = 2048      # +2KB since last summary
MIN_LINES_GROWTH = 50        # +50 lines since last summary
MILESTONE_RE = re.compile(
    r"\[milestone\]|\bepoch\b|\bstep\b|\bloss=|\baccuracy=|\bError\b|"
    r"\bTraceback\b|\bFAILED\b|\bDone\b|\b\[debug-round",
    re.IGNORECASE,
)

# Cap how much new text we send to Gemini per call (token control).
MAX_NEW_BYTES_TO_SEND = 8000

DEFAULT_IDLE_REPORT_SECONDS = 30 * 60
DEFAULT_STALE_REPORT_SECONDS = 2 * 60 * 60

# ---- runtime state --------------------------------------------------------

_should_stop = False


def _handle_sigterm(signum, frame):
    global _should_stop
    _should_stop = True
    print(f"[monitor] received signal {signum}, stopping...", file=sys.stderr)


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


# ---- gemini call ----------------------------------------------------------

def summarise(text: str) -> str:
    """把新增日志压成一段话。任何失败都降级成「没有摘要」，不抛出去。

    monitor 的心跳、idle/stale 检测不依赖模型，所以摘要拿不到时该少报一段，而不是让整个
    monitor 起不来。
    """
    prompt = (
        "You are watching an experiment log. Below is NEW content appended "
        "since last check. Output ONE paragraph (50 words max) summarizing "
        "what just happened. Focus on:\n"
        " - milestones (epochs / loss / metrics)\n"
        " - errors or warnings (only if real)\n"
        " - whether the run looks healthy / stalled / crashing\n\n"
        "Skip generic remarks. If nothing notable, say 'progress: routine'.\n\n"
        "=== NEW LOG CONTENT ===\n" + text
    )
    try:
        from llm_client import call_role

        return (call_role("run_monitor", prompt) or "").strip()
    except Exception as exc:
        print(f"[monitor] summary unavailable: {exc}", file=sys.stderr)
        return ""





# ---- watcher loop ---------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_notification(notify_log: Path, summary: str, ctx: dict) -> None:
    notify_log.parent.mkdir(parents=True, exist_ok=True)
    with notify_log.open("a") as f:
        f.write(
            f"[{now_iso()}] bytes={ctx['cur_bytes']} (+{ctx['delta_bytes']}) "
            f"lines={ctx['cur_lines']} (+{ctx['delta_lines']}) "
            f"trigger={ctx['trigger']}\n"
        )
        f.write(f"  {summary}\n\n")

def append_plain_notification(notify_log: Path, event: str, summary: str,
                              ctx: dict | None = None) -> None:
    notify_log.parent.mkdir(parents=True, exist_ok=True)
    fields = " ".join(f"{k}={v}" for k, v in (ctx or {}).items())
    suffix = f" {fields}" if fields else ""
    with notify_log.open("a") as f:
        f.write(f"[{now_iso()}] event={event}{suffix}\n")
        f.write(f"  {summary}\n\n")


def count_project_processes(project_root: Path | None) -> int:
    """Best-effort count of live commands that mention this project."""
    if not project_root:
        return 0
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,stat=,command="],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    needle = str(project_root)
    count = 0
    self_pid = os.getpid()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        stat = parts[1]
        cmd = parts[2]
        if pid == self_pid or "ar-gemini-monitor.py" in cmd:
            continue
        if "Z" in stat:
            continue
        if needle in cmd:
            count += 1
    return count


def write_state(state_path: Path | None, state: dict) -> None:
    if not state_path:
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(state_path)



def should_trigger(prev: dict, cur: dict, new_text: str) -> str | None:
    """Return reason string if we should call Gemini, else None."""
    if cur["lines"] - prev["lines"] >= MIN_LINES_GROWTH:
        return "lines_growth"
    if cur["bytes"] - prev["bytes"] >= MIN_BYTES_GROWTH:
        return "bytes_growth"
    if MILESTONE_RE.search(new_text):
        return "milestone"
    return None


def read_tail(path: Path, from_byte: int) -> tuple[str, int]:
    """Read from file starting at from_byte, return (text, current_size)."""
    size = path.stat().st_size
    if size <= from_byte:
        return "", size
    with path.open("rb") as f:
        f.seek(from_byte)
        # Cap how much we read into memory per cycle.
        text = f.read(MAX_NEW_BYTES_TO_SEND).decode("utf-8", errors="replace")
    return text, size


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", required=True, help="path to log file to monitor")
    ap.add_argument("--notify-log", default=None, help="where to append summaries")
    ap.add_argument("--project-root", default=None, help="run project root")
    ap.add_argument("--state", default=None, help="JSON heartbeat/status file")
    ap.add_argument("--summary", default=None, help="results summary file")
    ap.add_argument("--interval", type=int, default=30, help="poll seconds")
    ap.add_argument("--idle-report-seconds", type=int,
                    default=DEFAULT_IDLE_REPORT_SECONDS)
    ap.add_argument("--stale-report-seconds", type=int,
                    default=DEFAULT_STALE_REPORT_SECONDS)
    # 模型由 AR_MODEL_RUN_MONITOR 决定，与别的角色一致；不再单独一个 flag。
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = ap.parse_args()

    watch = Path(args.watch)
    notify = Path(args.notify_log or watch.parent / "notifications.log")
    project_root = Path(args.project_root).resolve() if args.project_root else None
    state_path = Path(args.state) if args.state else None
    summary_path = Path(args.summary) if args.summary else watch.parent / "summary.md"

    singleton_lock = None
    if project_root is not None:
        project_root.mkdir(parents=True, exist_ok=True)
        lock_path = project_root / ".ar-monitor.lock"
        singleton_lock = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(singleton_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"[monitor] already running for project_root={project_root}; exiting",
                file=sys.stderr,
            )
            return 0
        singleton_lock.seek(0)
        singleton_lock.truncate()
        singleton_lock.write(f"{os.getpid()}\n")
        singleton_lock.flush()

    # 摘要能不能出，等真的调一次才知道——凭据齐不齐由 call_role 那一侧判断，这里再查一遍
    # 就是第二份真相。拿不到时 summarise() 打一行到 stderr 并返回空串，monitor 照常跑。

    # Start at run initialization time. The runner may not exist yet, so create
    # the watched file instead of exiting after a short wait.
    watch.parent.mkdir(parents=True, exist_ok=True)
    if not watch.exists():
        watch.touch()

    prev = {"bytes": 0, "lines": 0}
    started_at = time.time()
    last_growth_at = started_at
    last_idle_report_at = 0.0
    last_stale_report_at = 0.0
    completion_reported = False
    append_plain_notification(
        notify,
        "monitor_started",
        "monitor started before runner work; waiting for log growth or summary.",
        {"watch": watch, "summary": summary_path},
    )
    print(
        f"[monitor] started watch={watch} notify={notify} "
        f"state={state_path or 'none'} interval={args.interval}s "
        f"summariser=call_role(run_monitor)",
        file=sys.stderr,
    )

    while not _should_stop:
        try:
            new_text, cur_bytes = read_tail(watch, prev["bytes"])
            cur_lines = prev["lines"] + new_text.count("\n")
            cur = {"bytes": cur_bytes, "lines": cur_lines}
            active_processes = count_project_processes(project_root)

            trigger = should_trigger(prev, cur, new_text) if new_text else None
            if trigger:
                last_growth_at = time.time()
                # 拿不到摘要时留一句占位，而不是把这一条记录整个丢掉：
                # notifications.log 的读者要能看出「这里本该有摘要」。
                summary = summarise(new_text) or "(summary unavailable)"
                if not summary:
                    summary = "(gemini call failed or empty)"
                append_notification(
                    notify,
                    summary,
                    {
                        "cur_bytes": cur_bytes,
                        "cur_lines": cur_lines,
                        "delta_bytes": cur_bytes - prev["bytes"],
                        "delta_lines": cur_lines - prev["lines"],
                        "trigger": trigger,
                    },
                )
                prev = cur
            elif new_text:
                # File grew but not enough to trigger; still update state so
                # next trigger compares against latest.
                last_growth_at = time.time()
                prev = cur
            else:
                idle_for = int(time.time() - last_growth_at)
                if (
                    idle_for >= args.idle_report_seconds
                    and time.time() - last_idle_report_at >= args.idle_report_seconds
                ):
                    append_plain_notification(
                        notify,
                        "idle",
                        "no new run.log content; monitor is still alive.",
                        {
                            "idle_seconds": idle_for,
                            "active_project_processes": active_processes,
                        },
                    )
                    last_idle_report_at = time.time()
                if (
                    idle_for >= args.stale_report_seconds
                    and time.time() - last_stale_report_at >= args.stale_report_seconds
                ):
                    append_plain_notification(
                        notify,
                        "stale",
                        "long experiment appears stalled or unattended.",
                        {
                            "idle_seconds": idle_for,
                            "active_project_processes": active_processes,
                        },
                    )
                    last_stale_report_at = time.time()

            if summary_path.exists() and not completion_reported:
                completion_reported = True
                append_plain_notification(
                    notify,
                    "summary_detected",
                    "results summary.md exists; runner likely finished and coordinator should run the result gate.",
                    {
                        "summary": summary_path,
                        "active_project_processes": active_processes,
                    },
                )

            write_state(
                state_path,
                {
                    "updated_at": now_iso(),
                    "started_at_epoch": int(started_at),
                    "watch": str(watch),
                    "notify_log": str(notify),
                    "summary": str(summary_path),
                    "bytes": cur["bytes"],
                    "lines": cur["lines"],
                    "idle_seconds": int(time.time() - last_growth_at),
                    "active_project_processes": active_processes,
                    "summary_detected": completion_reported,
                    "summariser": "call_role(run_monitor)",
                    "status": "running",
                },
            )
        except FileNotFoundError:
            print(f"[monitor] file disappeared, will retry: {watch}",
                  file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — log and keep going
            print(f"[monitor] cycle error: {e}", file=sys.stderr)

        if args.once:
            break
        # Sleep in 1-second chunks so signal handler responds quickly.
        for _ in range(args.interval):
            if _should_stop:
                break
            time.sleep(1)

    final_bytes = prev.get("bytes", 0)
    final_lines = prev.get("lines", 0)
    try:
        final_bytes = watch.stat().st_size
    except FileNotFoundError:
        pass
    write_state(
        state_path,
        {
            "updated_at": now_iso(),
            "started_at_epoch": int(started_at),
            "watch": str(watch),
            "notify_log": str(notify),
            "summary": str(summary_path),
            "bytes": final_bytes,
            "lines": final_lines,
            "idle_seconds": int(time.time() - last_growth_at),
            "summary_detected": completion_reported or summary_path.exists(),
            "summariser": "call_role(run_monitor)",
            "status": "stopped",
        },
    )
    append_plain_notification(notify, "monitor_stopped", "monitor exited cleanly.")
    print("[monitor] exited cleanly", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
