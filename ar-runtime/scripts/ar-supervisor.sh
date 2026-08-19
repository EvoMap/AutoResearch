#!/bin/bash
# AutoResearch 运行监督者：官方 claude 的 -p 会话可能死于终态 API 错误
# （如 DeepSeek 400 tool-use 配对拒绝），工作流本身靠 state.md + workflow_queue.json
# 可恢复，所以监督者只做一件事：没跑完就重启续跑。
#
# 用法: ar-supervisor.sh <idea_file> <project_root> [max_restarts] [attempt_timeout]
set -u
IDEA="$1"
PROJ="$2"
MAX_RESTARTS="${3:-8}"
ATTEMPT_TIMEOUT="${4:-6h}"
AR_MAX_CYCLES="${AR_MAX_CYCLES:-3}"
RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
# Print mode must keep the process group alive until asynchronous agents report
# back. A finite Claude Code ceiling kills the producer but leaves its unit running.
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0
ATTEMPT_PID=""
ATTEMPT_PGID_FILE=""
MANIFEST=""
MONITOR_PID=""
ATTEMPT_CLEAN=1
MONITORS_CLEAN=1

if ! [[ "$AR_MAX_CYCLES" =~ ^[1-9][0-9]*$ ]]; then
  echo "[supervisor] AR_MAX_CYCLES must be a positive integer; got: $AR_MAX_CYCLES" >&2
  exit 64
fi
export AR_MAX_CYCLES

# The controller gives GNU timeout or the direct runner its own session and
# publishes the PGID so the supervisor owns the complete attempt lifecycle.
run_with_timeout() {
  exec python3 - "$ATTEMPT_TIMEOUT" "$ATTEMPT_PGID_FILE" \
    "${AR_SUPERVISOR_TERM_GRACE:-10}" "$@" <<'PYEOF'
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

spec = sys.argv[1]
pgid_file = Path(sys.argv[2])
grace = float(sys.argv[3])
command = sys.argv[4:]
units = {"s": 1, "m": 60, "h": 3600}
seconds = float(spec[:-1]) * units[spec[-1]] if spec[-1] in units else float(spec)
timeout_bin = (None if os.environ.get("AR_SUPERVISOR_FORCE_PY_TIMEOUT") == "1"
               else shutil.which("timeout"))
backend = ([timeout_bin, f"--kill-after={grace}s", spec, *command]
           if timeout_bin else command)
blocked = {signal.SIGINT, signal.SIGTERM}
signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
proc = subprocess.Popen(backend, start_new_session=True)
pgid_file.write_text(f"{proc.pid}\n")


def group_exists() -> bool:
    try:
        os.killpg(proc.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS can report EPERM during the exit race after the group leader is
        # already reaped. An active leader still means the group is live.
        return proc.poll() is None


def wait_for_group(deadline: float) -> bool:
    while group_exists() and time.monotonic() < deadline:
        proc.poll()
        time.sleep(0.05)
    return not group_exists()


def terminate_group() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if not wait_for_group(time.monotonic() + grace):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=max(grace, 1))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if not wait_for_group(time.monotonic() + max(grace, 1)):
        raise RuntimeError(f"attempt process group {proc.pid} survived SIGKILL")


def forward(signum, _frame):
    terminate_group()
    pgid_file.unlink(missing_ok=True)
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, forward)
signal.signal(signal.SIGINT, forward)
signal.pthread_sigmask(signal.SIG_UNBLOCK, blocked)
try:
    rc = proc.wait() if timeout_bin else proc.wait(timeout=seconds)
except subprocess.TimeoutExpired:
    terminate_group()
    pgid_file.unlink(missing_ok=True)
    sys.exit(124)  # same code GNU timeout uses
terminate_group()
pgid_file.unlink(missing_ok=True)
sys.exit(rc)
PYEOF
}


is_done() {
  # 完成权威是引擎的只读复核，不是队列 JSON 的字面值：队列文件 coordinator 写得到，
  # 手改一个 status=done 就能骗过直接解析；AUTORESEARCH_DONE 同样是它写的（#143 #151）。
  python3 "$RUNTIME_DIR/scripts/ar-workflow-engine.py" verify-close --project-root "$PROJ" >/dev/null 2>&1
}

monitor_pids() {
  [ -n "${ABS_PROJ:-}" ] || return 0
  python3 - "$ABS_PROJ" <<'PYEOF'
import os
import subprocess
import sys

target = os.path.realpath(sys.argv[1])
out = subprocess.run(
    ["ps", "-eo", "pid=,command="], capture_output=True, text=True, check=True).stdout
for line in out.splitlines():
    if "ar-gemini-monitor" not in line:
        continue
    parts = line.split()
    try:
        pid = int(parts[0])
        arg = parts[parts.index("--project-root") + 1]
    except (ValueError, IndexError):
        continue
    # A relative argv path cannot be resolved after the process changes cwd.
    if os.path.isabs(arg) and os.path.realpath(arg) == target:
        print(pid)
PYEOF
}

reap_monitors() {
  # Startup and cleanup share this exact argv + realpath matcher.
  local monitor_pid=""
  local remaining=""
  local ticks=0
  local max_ticks=$(( ${AR_SUPERVISOR_TERM_GRACE:-10} * 10 ))
  for monitor_pid in $(monitor_pids 2>/dev/null || true); do
    kill -TERM "$monitor_pid" 2>/dev/null || true
  done
  remaining="$(monitor_pids 2>/dev/null || true)"
  while [ -n "$remaining" ] && [ "$ticks" -lt "$max_ticks" ]; do
    sleep 0.1
    ticks=$((ticks + 1))
    remaining="$(monitor_pids 2>/dev/null || true)"
  done
  if [ -n "$remaining" ]; then
    for monitor_pid in $remaining; do
      kill -KILL "$monitor_pid" 2>/dev/null || true
    done
  fi
  ticks=0
  remaining="$(monitor_pids 2>/dev/null || true)"
  while [ -n "$remaining" ] && [ "$ticks" -lt 100 ]; do
    sleep 0.1
    ticks=$((ticks + 1))
    remaining="$(monitor_pids 2>/dev/null || true)"
  done
  [ -z "$MONITOR_PID" ] || wait "$MONITOR_PID" 2>/dev/null || true
  if [ -n "$remaining" ]; then
    echo "[supervisor] project monitors survived SIGKILL; manifest not sealed: $remaining" >&2
    MONITORS_CLEAN=0
  fi
}

stop_attempt() {
  [ -n "$ATTEMPT_PID" ] || return 0
  local pgid=""
  if [ -n "$ATTEMPT_PGID_FILE" ] && [ -s "$ATTEMPT_PGID_FILE" ]; then
    pgid="$(sed -n '1p' "$ATTEMPT_PGID_FILE")"
  fi
  if kill -0 "$ATTEMPT_PID" 2>/dev/null; then
    kill -TERM "$ATTEMPT_PID" 2>/dev/null || true
  fi
  case "$pgid" in
    ''|*[!0-9]*) ;;
    *) kill -TERM -- "-$pgid" 2>/dev/null || true ;;
  esac

  local ticks=0
  local max_ticks=$(( ${AR_SUPERVISOR_TERM_GRACE:-10} * 10 ))
  while kill -0 "$ATTEMPT_PID" 2>/dev/null && [ "$ticks" -lt "$max_ticks" ]; do
    sleep 0.1
    ticks=$((ticks + 1))
  done
  case "$pgid" in
    ''|*[!0-9]*) ;;
    *) kill -KILL -- "-$pgid" 2>/dev/null || true ;;
  esac
  if kill -0 "$ATTEMPT_PID" 2>/dev/null; then
    kill -KILL "$ATTEMPT_PID" 2>/dev/null || true
  fi
  wait "$ATTEMPT_PID" 2>/dev/null || true

  ticks=0
  case "$pgid" in
    ''|*[!0-9]*) ;;
    *)
      while kill -0 -- "-$pgid" 2>/dev/null && [ "$ticks" -lt 100 ]; do
        sleep 0.1
        ticks=$((ticks + 1))
      done
      if kill -0 -- "-$pgid" 2>/dev/null; then
        echo "[supervisor] attempt process group $pgid survived SIGKILL; manifest not sealed" >&2
        ATTEMPT_CLEAN=0
      fi
      ;;
  esac
  ATTEMPT_PID=""
  [ -z "$ATTEMPT_PGID_FILE" ] || rm -f "$ATTEMPT_PGID_FILE"
  ATTEMPT_PGID_FILE=""
}

on_exit() {
  local rc=$?
  local final_rc=$rc
  local finish_output=""
  local finish_rc=0
  # Seal the manifest only after the independent attempt group is gone and waited.
  if [ -n "$ATTEMPT_PID" ]; then
    stop_attempt
  fi
  # Monitors can write state during shutdown, so they must stop before hashing.
  reap_monitors
  if [ -n "$MANIFEST" ] && [ "$ATTEMPT_CLEAN" -eq 1 ] && [ "$MONITORS_CLEAN" -eq 1 ]; then
    cd "$RUNTIME_DIR" 2>/dev/null || true
    finish_output="$(python3 ../scripts/ar_run_manifest.py finish --project-root "$PROJ" \
      --attempt "$MANIFEST" --exit-code "$rc" 2>&1)"
    finish_rc=$?
    if [ "$finish_rc" -eq 0 ]; then
      [ -z "$finish_output" ] || printf '%s\n' "$finish_output"
    else
      echo "[supervisor] run manifest finish failed rc=$finish_rc: $finish_output" >&2
      [ "$rc" -ne 0 ] || final_rc=$finish_rc
    fi
  elif [ -n "$MANIFEST" ] && [ "$rc" -eq 0 ]; then
    final_rc=2
  fi
  if [ "$final_rc" -ne "$rc" ]; then
    trap - EXIT
    exit "$final_rc"
  fi
}
# 人工中断、CI cancel、SSH 断开也要收尾：signal trap 换成对应退出码后走同一个 EXIT 路径。
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

cd "$RUNTIME_DIR" || exit 1
mkdir -p "$PROJ/results"
ABS_PROJ="$(cd "$PROJ" && pwd)"

# The supervisor owns the run budget. A coordinator that omits --max-cycles
# must not silently fall back to the engine default before the first unit runs.
if ! is_done; then
  init_output="$(python3 "$RUNTIME_DIR/scripts/ar-workflow-engine.py" init \
    --project-root "$ABS_PROJ" --max-cycles "$AR_MAX_CYCLES" 2>&1)"
  init_rc=$?
  if [ "$init_rc" -ne 0 ]; then
    echo "[supervisor] workflow init failed rc=$init_rc: $init_output" >&2
    exit "$init_rc"
  fi
fi

# Each invocation owns one attempt manifest. Without a successful start there is
# no auditable run, so fail closed and never fall back to the legacy manifest.
manifest_output="$(python3 ../scripts/ar_run_manifest.py start --project-root "$PROJ" \
  --runner "$CLAUDE_BIN" --idea "$IDEA" 2>&1)"
manifest_rc=$?
if [ "$manifest_rc" -ne 0 ]; then
  echo "[supervisor] run manifest start failed rc=$manifest_rc: $manifest_output" >&2
  exit "$manifest_rc"
fi
MANIFEST="$(printf '%s\n' "$manifest_output" | sed -n 's/^run manifest started: //p')"
if [ -z "$MANIFEST" ]; then
  echo "[supervisor] run manifest start returned no attempt path" >&2
  exit 2
fi

# monitor 由 supervisor 拉起并持有 PID；coordinator 看到 AR_SUPERVISOR_MONITOR=1 就不再
# 自己起一份（SKILL Phase 0 第 9 步）。已有同项目 monitor 在跑时不重复拉。
export AR_SUPERVISOR_MONITOR=1
touch "$ABS_PROJ/results/run.log"
if [ -z "$(monitor_pids 2>/dev/null || true)" ]; then
  python3 "$RUNTIME_DIR/scripts/ar-gemini-monitor.py" \
    --project-root "$ABS_PROJ" \
    --watch "$ABS_PROJ/results/run.log" \
    --summary "$ABS_PROJ/results/summary.md" \
    --notify-log "$ABS_PROJ/results/notifications.log" \
    --state "$ABS_PROJ/results/monitor_state.json" \
    --interval 60 >/dev/null 2>&1 &
  MONITOR_PID=$!
  echo "[supervisor] $(date -u +%Y-%m-%dT%H:%M:%SZ) monitor started pid=$MONITOR_PID"
fi

for attempt in $(seq 1 "$MAX_RESTARTS"); do
  if is_done; then
    echo "[supervisor] $(date -u +%Y-%m-%dT%H:%M:%SZ) workflow complete (close_if_done=done), exiting"
    exit 0
  fi
  echo "[supervisor] $(date -u +%Y-%m-%dT%H:%M:%SZ) attempt ${attempt}/${MAX_RESTARTS} starting coordinator"
  # 显式带上 project_root：is_done/manifest 盯的就是 $PROJ，不能靠 coordinator 重新推导。
  # attempt 放后台、用可中断的 wait 等：bash 在前台命令期间会推迟 trap，SIGTERM 要等
  # 6h attempt 跑完才生效——实测中断后 monitor 存活的根源。
  ATTEMPT_PGID_FILE="$(mktemp "${TMPDIR:-/tmp}/ar-supervisor-attempt.XXXXXX")"
  run_with_timeout "$CLAUDE_BIN" --dangerously-skip-permissions \
    -p "/ar-coordinator $IDEA $PROJ" &
  ATTEMPT_PID=$!
  wait "$ATTEMPT_PID"
  rc=$?
  stop_attempt
  echo "[supervisor] $(date -u +%Y-%m-%dT%H:%M:%SZ) coordinator exited rc=$rc"
  if is_done; then
    echo "[supervisor] $(date -u +%Y-%m-%dT%H:%M:%SZ) workflow complete after attempt ${attempt}"
    exit 0
  fi
  sleep "${AR_SUPERVISOR_RETRY_SLEEP:-20}"
done
echo "[supervisor] $(date -u +%Y-%m-%dT%H:%M:%SZ) max restarts (${MAX_RESTARTS}) exhausted, giving up — needs human"
exit 2
