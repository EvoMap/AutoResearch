#!/usr/bin/env bash
# Are the two MCP servers the coordinator depends on able to serve?
#
# One entry point for both callers. Phase 0 of the ar-coordinator skill runs this,
# and so does CI. When the skill and CI called the servers separately, CI proved
# the self-tests behaved and proved nothing about whether the coordinator invoked
# them correctly -- which is exactly what went wrong twice: the servers reported
# their state accurately while the skill changed into its current directory again,
# and appended `; echo "rc=$?"`, which replaces the exit status the
# caller sees with the echo's own zero.
#
# It resolves its own location so bringup, CI, and the coordinator invoke the same entry point.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$CLAUDE_ROOT/.." && pwd)"
cd "$CLAUDE_ROOT" || exit 1

BUN="$("$REPO_ROOT/scripts/find-bun.sh")"
if [ -z "$BUN" ]; then
  # Not the same failure as "the server cannot serve". Reporting it as a missing
  # credential would send the operator to settings.local.json for a problem that
  # is not there.
  #
  # This script spawns bun itself, so off-PATH is fine here and find-bun.sh's exit
  # 3 is not an error: only "nowhere on this machine" stops the self-test.
  echo "bun 未安装，无法运行 MCP self-test" >&2
  echo "已检查：PATH 和 ~/.bun/bin" >&2
  echo "下一步：装 bun（https://bun.sh），或在已装 bun 的环境里跑" >&2
  exit 2
fi

fail=0

# Reviewer and critic load .env through the Python bridge. settings.local.json only configures the
# Claude Code main loop; importing its old provider keys here would recreate the retired authority.
projection=$(cd "$REPO_ROOT" && scripts/hook_python.sh -c '
import sys
sys.path.insert(0, "scripts")
import render_env
ok, reason = render_env.local_is_fresh()
print(reason)
raise SystemExit(0 if ok else 1)
' 2>&1)
projection_rc=$?
if [ "$projection_rc" -ne 0 ]; then
  printf 'Claude Code 本机投影不可用：%s\n' "$projection" >&2
  printf '下一步：在仓库根目录加载 .env 后运行 scripts/hook_python.sh scripts/render_env.py\n' >&2
  fail=1
fi

run_one() {
  local label="$1" script="$2" required="$3"
  local out rc
  out=$("$BUN" "$script" --self-test 2>&1)
  rc=$?
  printf '%s\n' "$out"
  if [ "$rc" -ne 0 ]; then
    printf '  %s 不可用（exit %s）\n' "$label" "$rc" >&2
    [ "$required" = "required" ] && fail=1
  fi
  return 0
}

run_one "Step 3 reviewer (code_reviewer role)" scripts/ar-gemini-review-mcp.ts required
run_one "Step 5 critic (ar-external-critic)" scripts/ar-external-critic-mcp.ts required

if [ "$fail" -ne 0 ]; then
  cat >&2 <<'MSG'

上面的 JSON 会列出角色、候选模型和当前可用模型。
在 config/providers.local.json 里为对应角色选择模型，并在仓库根目录 .env
填写该模型 route 引用的 URL/key 环境变量，然后重跑。
MSG
  exit 1
fi

echo "MCP preflight: 两个 server 都可用"
