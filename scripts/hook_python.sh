#!/usr/bin/env bash
# 找一个能跑仓内检查脚本的解释器，然后把参数交给它。
#
# 不写死 `.venv/bin/python`：linked worktree 里没有那个目录（git 不复制 ignored 目录），
# 于是 hook 在检查任何代码之前就以「Executable not found」失败，而仓库自己要求用
# worktree 并行开发（#119）。
#
# 顺序是「这个树自己的 venv → 主工作树的 venv → 能 import 依赖的解释器」。最后一档
# 存在的理由：检查脚本 import httpx，而系统 python 常常没有；找不到时说清怎么办，
# 不静默跳过——跳过的 hook 和没有 hook 一样。
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
common="$(git rev-parse --git-common-dir)"
main_tree="$(cd "$(dirname "$common")" && pwd)"

for candidate in "$repo/.venv/bin/python" "$main_tree/.venv/bin/python"; do
    if [ -x "$candidate" ]; then exec "$candidate" "$@"; fi
done

for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c "import httpx" >/dev/null 2>&1; then
        exec "$candidate" "$@"
    fi
done

cat >&2 <<'MSG'
找不到能跑仓内检查的解释器。这些检查 import httpx 等依赖，系统 python 通常没有。

  bash scripts/bringup.sh        # 在这个树里建 .venv 并装依赖

linked worktree 也可以直接用主工作树的 .venv，本脚本会自动找到它。
MSG
exit 1
