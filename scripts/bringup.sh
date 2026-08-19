#!/usr/bin/env bash
# 一条命令回答：这台机器现在能不能跑 AutoResearch，不能的话缺什么。
#
# 零 flag。能探到的都探，探不到的说清楚缺什么、去哪补。跑完输出一段可以原样贴回来的
# SUMMARY，不用再追问"那边到底什么情况"。
#
# 两条轨道分开表态：出 idea 和跑实验的门不一样，一条能跑另一条不能是常见状态。
#
# 退出码只看「出 idea」那条，因为它是每台机器都要的那半边。实验是否需要 GPU 由 Idea
# 决定；这里的 GPU 探测只报告机器能力。跑实验那条能不能启动，在 SUMMARY 里单独写清楚。
#
# 退出码：
#   0  出 idea 可以跑（跑实验能不能，看 SUMMARY 第二行）
#   1  出 idea 缺了必需的东西（SUMMARY 里列了是哪些）
#   2  这个脚本自己没法继续（不在仓库根、没有 python3）
#
# 不碰 secret：只报告哪个环境变量是空的，不打印值，也不写进日志。

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 2

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/bringup_$STAMP.log"
STARTED_AT="$(date -u +%s)"

# 每一项记成 "状态|名字|说明"，SUMMARY 从这里长出来，不靠人肉汇总。
RESULTS=""
BLOCKERS=0
EXEC_BLOCKERS=0

# 两条轨道分开记：这台机器能不能出 idea，和能不能跑实验，是两个问题。合成一个
# "verdict 可以跑" 会让一台装不了 bun 的机器被告知去跑实验。
#
# 第四个参数说这一项影响哪条轨道：省略 = 两条都影响，"exec" = 只影响跑实验。
# fail 一律两条都算：测试挂了、凭证缺了，两边都别跑。
note() {  # note <ok|warn|fail> <名字> <说明> [exec]
    RESULTS="$RESULTS
$1|$2|$3"
    case "$1" in
        ok)   printf '  \033[32m✓\033[0m %-26s %s\n' "$2" "$3" ;;
        warn) printf '  \033[33m!\033[0m %-26s %s\n' "$2" "$3"
              [ "${4:-}" = "exec" ] && EXEC_BLOCKERS=$((EXEC_BLOCKERS + 1)) ;;
        fail) printf '  \033[31m✗\033[0m %-26s %s\n' "$2" "$3"
              BLOCKERS=$((BLOCKERS + 1)); EXEC_BLOCKERS=$((EXEC_BLOCKERS + 1)) ;;
    esac
    return 0
}

section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# 所有输出同时进日志。exec 之后 stdout 是 tee，颜色码也会进文件，看日志时用 less -R。
exec > >(tee -a "$LOG") 2>&1

printf '\033[1mAutoResearch bringup\033[0m  %s  %s\n' "$STAMP" "$REPO"

# ---------------------------------------------------------------- 1. 凭证来源
section "1. 凭证"

if [ -f "$REPO/.env" ]; then
    # set -a 让 source 进来的变量自动 export，否则子进程（pytest、preflight）看不到。
    set -a
    # shellcheck disable=SC1091  # 这个文件按设计不进 git
    . "$REPO/.env"
    set +a
    note ok "env-file" "已加载 .env（$(grep -cE "^[A-Z0-9_]+=" "$REPO/.env" 2>/dev/null; true) 个赋值）"
else
    note warn "env-file" "没有 .env。cp .env.example .env 然后填，或者自己 export"
fi

# 要哪些变量取决于当前生效的那份 config，硬编码清单会逐渐和 config 分叉。
CONFIG="${AUTORESEARCH_CONFIG:-}"
if [ -z "$CONFIG" ]; then
    if [ -f "$REPO/config/providers.local.json" ]; then
        CONFIG="config/providers.local.json"
    else
        CONFIG="config/providers.example.json"
    fi
fi
note ok "config" "$CONFIG"

# ---------------------------------------------------------------- 2. 机器
section "2. 机器"

note ok "os" "$(uname -s) $(uname -m)"

if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_LINE="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1)"
    GPU_COUNT="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l | tr -d ' ')"
    if [ -n "$GPU_LINE" ]; then
        note ok "gpu" "$GPU_COUNT 张，$GPU_LINE"
    else
        note warn "gpu" "有 nvidia-smi 但列不出显卡；CPU 实验仍可运行，需要 CUDA 的 Idea 会失败"
    fi
else
    note warn "gpu" "没有 nvidia-smi；CPU 实验仍可运行，需要 CUDA 的 Idea 会失败"
fi

DISK_FREE="$(df -Pk "$REPO" 2>/dev/null | awk 'NR==2 {printf "%.0f", $4/1048576}')"
if [ -n "$DISK_FREE" ] && [ "$DISK_FREE" -lt 20 ] 2>/dev/null; then
    note warn "disk" "${DISK_FREE}G 可用，模型权重和产出可能不够"
else
    note ok "disk" "${DISK_FREE:-?}G 可用"
fi

# ---------------------------------------------------------------- 3. 运行时
section "3. 运行时"

if command -v python3 >/dev/null 2>&1; then
    note ok "python" "$(python3 --version 2>&1 | awk '{print $2}')"
else
    printf '\n没有 python3，这个脚本没法继续。\n已检查：PATH 里的 python3\n下一步：装 Python 3.10+ 再重跑\n'
    exit 2
fi

VENV="$REPO/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    printf '  建 venv…\n'
    python3 -m venv "$VENV" >/dev/null 2>&1 || {
        note fail "venv" "python3 -m venv 失败，可能缺 python3-venv"
    }
fi
PY="$VENV/bin/python"
[ -x "$PY" ] || PY="python3"

# 判据是 requirements.txt 的指纹，不是「能不能 import httpx」。只探一个包的话，
# 升级一个已有 clone 时新增的依赖看不见：bringup 报 deps 已装，紧接着测试报
# ModuleNotFoundError（#132）。指纹存在 venv 里，装完才写。
REQ_STAMP="$VENV/.requirements.sha256"
REQ_NOW="$(shasum -a 256 "$REPO/requirements.txt" 2>/dev/null | cut -d' ' -f1)"
if [ -z "$REQ_NOW" ]; then
    REQ_NOW="$(sha256sum "$REPO/requirements.txt" 2>/dev/null | cut -d' ' -f1)"
fi

if [ -n "$REQ_NOW" ] && [ "$(cat "$REQ_STAMP" 2>/dev/null)" = "$REQ_NOW" ] &&
   "$PY" -c "import httpx" >/dev/null 2>&1; then
    note ok "deps" "已装（requirements 未变）"
else
    printf '  装依赖…（首次会慢）\n'
    # uv 建的 venv 里没有 pip。装不了的时候要说清是「这个 venv 没有 pip」还是「装失败」，
    # 否则一台用 uv 的机器上看到的是「pip install 失败」，照着去查网络和源。
    INSTALL_OK=0
    if "$PY" -m pip --version >/dev/null 2>&1; then
        "$PY" -m pip install -q -r "$REPO/requirements.txt" 2>&1 | tail -3 && INSTALL_OK=1
    elif command -v uv >/dev/null 2>&1; then
        uv pip install -q --python "$PY" -r "$REPO/requirements.txt" 2>&1 | tail -3 && INSTALL_OK=1
    else
        note fail "deps" "这个 venv 没有 pip，也没装 uv。装一个再跑（https://astral.sh/uv）"
    fi
    if [ "$INSTALL_OK" = 1 ]; then
        [ -n "$REQ_NOW" ] && printf '%s' "$REQ_NOW" > "$REQ_STAMP"
        note ok "deps" "已装"
    else
        note fail "deps" "pip install 失败，看上面几行"
    fi
fi

# bun 只有 ar-runtime 的两个 MCP server 要，装 idea 管线用不上。
#
# 三种状态，不是两种。ar-runtime/.mcp.json 里写的是 `"command": "bun"`，Claude Code 按
# PATH 找，所以「装在 ~/.bun/bin 但不在 PATH 上」和「没装」对 MCP server 的后果一样，
# 下一步却完全不同：前者要改 PATH，照 https://bun.sh 再装一遍还是同一个结果，因为安装器
# 写的就是那个位置。这三种状态由 find-bun.sh 的退出码给，不在这里重判一遍。
BUN_PATH="$("$REPO/scripts/find-bun.sh")"
case $? in
    0) note ok "bun" "$("$BUN_PATH" --version 2>/dev/null)" ;;
    3) note warn "bun" "装在 ~/.bun/bin 但不在 PATH 上，MCP server 按 PATH 找不到它。
                            把 export PATH=\"\$HOME/.bun/bin:\$PATH\" 加进 shell 配置" exec ;;
    *) note warn "bun" "没装。coordinator 的 MCP server 跑不了（https://bun.sh）" exec ;;
esac

# ar-runtime 的执行进程由官方 Claude Code CLI 提供。缺它不影响出 idea，但 supervisor
# 到真正启动时才报 command not found，会把配置问题伪装成一次运行失败。
if command -v claude >/dev/null 2>&1; then
    if CLAUDE_VERSION="$(claude --version 2>&1)" && [ -n "$CLAUDE_VERSION" ]; then
        note ok "claude" "$(printf '%s\n' "$CLAUDE_VERSION" | head -1)"
    else
        note warn "claude" "命令存在但 claude --version 失败。重装：
                            npm install -g @anthropic-ai/claude-code" exec
    fi
else
    note warn "claude" "没装官方 Claude Code CLI。安装：
                            npm install -g @anthropic-ai/claude-code" exec
fi

# ---------------------------------------------------------------- 4. 代码自检
section "4. 代码自检"

# 跟 CI 收同一批。只跑 tests/ 的话本地绿不代表 CI 绿：workflow engine 的协议测试在
# ar-runtime/scripts/tests/ 下，两边差 12 条。有测试盯着这两处不再分叉。
#
# 只把真实存在的目录传给 pytest。缺目录时 pytest 直接报错，而「这台机器上没有那棵树」
# 是合法状态。裁剪过 vendored 树的部署和测试沙箱都会命中。
TEST_PATHS=""
for candidate in tests/ ar-runtime/scripts/tests/; do
    [ -d "$REPO/$candidate" ] && TEST_PATHS="$TEST_PATHS $candidate"
done
if "$PY" -m pytest $TEST_PATHS -q >/tmp/bringup_pytest.$$ 2>&1; then
    note ok "tests" "$(grep -oE '[0-9]+ passed' /tmp/bringup_pytest.$$ | tail -1)"
else
    note fail "tests" "$(grep -oE '[0-9]+ failed[^ ]*' /tmp/bringup_pytest.$$ | tail -1) — 详见 $LOG"
    tail -15 /tmp/bringup_pytest.$$
fi
rm -f /tmp/bringup_pytest.$$

if "$PY" scripts/secret_scan.py . >/dev/null 2>&1; then
    note ok "secret-scan" "没有未审阅的形态"
else
    note fail "secret-scan" "有命中，跑 python scripts/secret_scan.py . 看"
fi

if "$PY" scripts/check_public_knowledge.py . >/dev/null 2>&1; then
    note ok "public-kb" "知识文件与发布清单一致"
else
    note fail "public-kb" "知识文件越过发布清单，跑 python scripts/check_public_knowledge.py 看"
fi

# ---------------------------------------------------------------- 5. 凭证够不够
section "5. 凭证够不够跑"

PREFLIGHT_OUT="$("$PY" scripts/preflight.py --config "$CONFIG" 2>&1)"
PREFLIGHT_RC=$?
if [ "$PREFLIGHT_RC" -eq 0 ]; then
    note ok "preflight" "每个角色都有可用的 provider"
else
    # 操作者要的是「去 .env 里补哪几个变量」，不是「几个角色没解决」。
    # 分隔符有三种：逗号、` or `、以及 `+`（preflight 用它表示「这几个要一起给」，
    # 例如 `either (A or B + C) or D`）。漏掉 + 时，只以这种形态出现的变量会被丢掉。
    MISSING="$(printf '%s' "$PREFLIGHT_OUT" \
        | grep -oE 'needs .*' | sed 's/^needs //' \
        | tr ',+' '\n\n' | sed 's/ or /\n/g; s/[()]//g; s/^ *//; s/ *$//' \
        | grep -E '^[A-Z][A-Z0-9_]+$' | sort -u | tr '\n' ' ')"
    ROLES="$(printf '%s' "$PREFLIGHT_OUT" | grep -oE '^[0-9]+ required' | grep -oE '^[0-9]+')"
    # 点名是哪几个角色。只报数量时，一份「部分可用」的凭证看起来和「完全没配」一样，
    # 而这两件事要做的补救完全不同（#50）。
    UNRESOLVED="$(printf '%s' "$PREFLIGHT_OUT" \
        | grep -oE 'required role\(s\) unresolved: .*' | sed 's/.*unresolved: //')"
    note fail "preflight" "${ROLES:-有} 个角色没有可用 provider${UNRESOLVED:+：$UNRESOLVED}"
    # Show the minimum protocol-complete setup before the exhaustive preflight list.
    # One gateway may expose both protocols, but the official CLI does not speak OpenAI Chat.
    printf '        最省的配法：OpenAI Chat + Anthropic Messages 两种协议入口\n'
    printf '            OPENAI_BASE_URL=https://<你的端点>/v1\n'
    printf '            OPENAI_API_KEY=<key>\n'
    printf '            ANTHROPIC_BASE_URL=https://<你的端点>\n'
    printf '            ANTHROPIC_API_KEY=<key>\n'
    printf '        已经有别家 key 就直接用，各角色分别认哪些：python scripts/preflight.py\n'
    printf '        （涉及的全部变量：%s）\n' "${MISSING:-见 preflight 输出}" 
fi

# 本机那份投影含真实凭据，CI 看不见，所以新鲜度只能在这里查。
FRESH="$("$PY" -c 'import sys; sys.path.insert(0, "scripts"); import render_env;
ok, why = render_env.local_is_fresh(); print(("ok " if ok else "warn ") + why)' 2>/dev/null)"
case "$FRESH" in
    ok*)   note ok "projection" "${FRESH#ok }" ;;
    warn*) note warn "projection" "${FRESH#warn }" ;;
    *)     note warn "projection" "查不了（跑 .venv/bin/python scripts/render_env.py --check 看）" ;;
esac

if [ -x "$REPO/ar-runtime/scripts/ar-preflight-mcp.sh" ]; then
    bash "$REPO/ar-runtime/scripts/ar-preflight-mcp.sh" >/dev/null 2>&1
    RC=$?
    if [ "$RC" -eq 0 ]; then
        note ok "mcp" "两个都可用"
    else
        if [ "$RC" -eq 2 ]; then
            note warn "mcp" "跳过（没有 bun）" exec
        else
            note fail "mcp" "至少一个不可用，跑 ar-runtime/scripts/ar-preflight-mcp.sh 看"
        fi
    fi
fi

# ---------------------------------------------------------------- 版本状态
section "版本状态"

note ok "version" "$(git -C "$REPO" describe --tags --always --dirty 2>/dev/null || echo '?')"

# ---------------------------------------------------------------- 提交前的门禁
# 装没装是能探出来的，不留给人猜。git 不允许仓库替你设 core.hooksPath，所以这一步
# 只能由每个 clone 自己跑一次；不报出来的话，本地门禁不存在这件事要等 CI 红了才知道。
section "提交前的门禁"
if [ ! -f "$REPO/.pre-commit-config.yaml" ]; then
    note warn "pre-commit" "仓里没有配置，跳过"
elif [ -f "$REPO/.git/hooks/pre-commit" ] && grep -q pre-commit "$REPO/.git/hooks/pre-commit" 2>/dev/null; then
    note ok "pre-commit" "已安装"
else
    note warn "pre-commit" "未安装，跑 pip install pre-commit && pre-commit install"
fi

# ---------------------------------------------------------------- SUMMARY
ELAPSED=$(( $(date -u +%s) - STARTED_AT ))
section "SUMMARY（可原样贴回）"

{
    echo "AutoResearch bringup $STAMP"
    echo "host      $(uname -s) $(uname -m)  $(hostname 2>/dev/null || echo '?')"
    echo "commit    $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')"
    # 升级类问题第一句要问的就是「你在哪个版本」。commit 回答不了：贴回来的人不知道
    # 那个 hash 落在哪一版之后。
    echo "version   $(git -C "$REPO" describe --tags --always --dirty 2>/dev/null || echo '?')"
    echo "config    $CONFIG"
    echo "elapsed   ${ELAPSED}s"
    echo "log       $LOG"
    echo
    printf '%s\n' "$RESULTS" | awk -F'|' 'NF==3 {printf "%-5s %-26s %s\n", $1, $2, $3}'
    echo
    # 两条轨道各自表态。一条能跑另一条不能，是常见状态：Idea pipeline 不需要 Bun 或
    # Claude Code，而 runtime 需要。合成一句会把其中一半说错。
    if [ "$BLOCKERS" -eq 0 ]; then
        echo "verdict   出 idea    READY      .venv/bin/python idea_generation.py"
    else
        echo "verdict   出 idea    BLOCKED    还差 $BLOCKERS 项（上面标 fail 的）"
    fi
    if [ "$EXEC_BLOCKERS" -eq 0 ]; then
        echo "          跑实验    READY      在 ar-runtime/ 下 ./scripts/ar-supervisor.sh <idea> <project>"
    else
        echo "          跑实验    BLOCKED    还差 $EXEC_BLOCKERS 项（上面标 fail 或 warn 的）"
    fi
} | tee "$LOG_DIR/bringup_summary_$STAMP.txt"

[ "$BLOCKERS" -eq 0 ] || exit 1
