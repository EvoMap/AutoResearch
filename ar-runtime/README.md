# ar-runtime：执行工作流（Claude Code 与 Grok）

AutoResearch 的执行 runtime：把一份 Idea 推进为计划、代码、实验结果和独立评审。
确定性队列仍由 `scripts/ar-workflow-engine.py` 拥有。有两套 harness：

- 官方 `claude` CLI 加载本目录 `.claude/` 的 agents、skills 与 MCP。
- Grok Build 加载仓库根 `.grok/` 的 agents、skills、workflows，以及本目录
  `.grok/config.toml` / `.mcp.json` 的 MCP。

## 前置

1. 官方 `claude` CLI 已安装并可在 PATH 找到（`npm install -g @anthropic-ai/claude-code`）。
   每次 run 的实际版本由 supervisor 起跑时写进 `<project_root>/run_manifest.json`。
2. Bun ≥ 1.3（两个 MCP server 用它跑）：`cd ar-runtime && bun install --frozen-lockfile`。
3. 仓库根的 Python venv（engine 纯 stdlib，monitor 走仓根 `src/`）：`bash scripts/bringup.sh`。
4. ralph-loop 插件已安装（`.claude/settings.json` 已声明启用；未安装时 Claude Code 会提示）。

## 凭据

两种给法任选：

- 直接在 shell 导出 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`、
  `GEMINI_BASE_URL` / `GEMINI_API_KEY` / `GEMINI_REVIEW_MODEL` / `GEMINI_CRITIC_MODEL`、
  可选 `GPT_CRITIC_*`，再启动 `claude`（MCP 子进程继承环境）。
- 或用投影：`python scripts/render_env.py` 把托管键写进本目录的
  `.claude/settings.local.json`（git 忽略；本机路由放 `config/providers.local.json`）。
  `scripts/bringup.sh` 的新鲜度检查会在配置、环境或投影代码变化后要求重新生成。

自检（凭据齐不齐、两个 MCP 能不能服务）：

    ./scripts/ar-preflight-mcp.sh

## 启动

    cd ar-runtime
    claude --dangerously-skip-permissions \
      -p "/ar-coordinator ../examples/ideas/synthetic_gpu_smoke.md ../data/projects/<新目录>"

`-p` 非交互模式每次调用推进队列里的一个单元，整条队列靠 ralph-loop 自动续跑；会话死掉
时用监督者兜底重启：

    ./scripts/ar-supervisor.sh ../examples/ideas/synthetic_gpu_smoke.md ../data/projects/<新目录>

判断真实进度读 `<project_root>/state.md` 的 `## agents` 段和实际产物，不要只信
`workflow_queue.json` 的 status。

## Grok

Skills / agents / workflows 在仓库根 `.grok/`（Grok 从 cwd 向上走到 git root 都会发现）。
在仓库根或 `ar-runtime/` 启动均可。

交互式：

```text
cd /path/to/AutoResearch
grok
/ar-coordinator examples/ideas/synthetic_gpu_smoke.md data/projects/<新目录>
```

引擎驱动的整条队列（Grok 并行 claim 池，替代 ralph-loop）：

```text
/ar-coordinator
```

workflow 参数（`/workflows` 看进度）：

```text
args.idea_file = examples/ideas/synthetic_gpu_smoke.md
args.project_root = data/projects/<新目录>
args.max_parallel = 8    # ready-front 每波 worker 数，1–32
args.max_waves = 24
```

已编码实验的并行种子/消融：`/ar-experiment-matrix`（`args.project_root` + `args.items`，默认最多 16 路，上限 32）。

非交互：

```bash
grok --yolo -p "/ar-coordinator examples/ideas/synthetic_gpu_smoke.md data/projects/<新目录>"
```

父会话和 workflow 用 `parallel()` / 同轮 `spawn_subagent` 铺 ready-front。子 agent
不能嵌套，所以大模块的 `subcoder_requests` 由父级 fan-out `ar-subcoder`；claim
worker 作为叶子则自己写完模块。Reviewer / critic 通过 `search_tool` + `use_tool`
调 MCP。
