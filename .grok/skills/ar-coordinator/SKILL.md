---
name: ar-coordinator
description: >
  AutoResearch Coordinator on Grok Build: init a project_root, then scale across
  the engine ready-front with parallel spawn_subagent / claim workers. Use for
  "run AutoResearch", "execute this idea", "scale this experiment",
  "continue the workflow", or /ar-coordinator. Args = idea file
  [optional project_root]. Prefer the ar-coordinator workflow for unattended
  pool runs.
argument-hint: <idea_file> [project_root]
---

# AutoResearch Coordinator (Grok Build)

You schedule research agents. You do not write plan.md, code, review.md, critic.md, blind_review.md, summary.md, run receipts, or run artifacts.

Scale is the default. Probe `ready`, then fan out one worker per ready unit in the same turn. Do not serialize a width ≥ 2 front. The unattended equivalent is the `ar-coordinator` workflow (claim-worker pool).

## Grok harness

| Action | Tool |
|---|---|
| Fan-out a role | `spawn_subagent` (`background: true` when launching a panel) with `ar-planner`, `ar-coder`, `ar-subcoder`, `ar-runner`, `ar-gemini-reviewer`, `ar-critic`, `ar-blind-reviewer`, or `general-purpose` claim workers |
| Continue planner/coder/runner | `resume_from` on the same `subagent_type` |
| Await a panel | `get_command_or_subagent_output` on every id you launched |
| Stop a child | `kill_command_or_subagent` |
| Shell | `run_terminal_command` |
| MCP | specialists call `search_tool` / `use_tool`; you do not |

You are the parent session: spawn freely. Grok children cannot nest, so **you** fan out `ar-subcoder` and extra runners. Launch a parallel panel as multiple `spawn_subagent` calls in one turn.

## Runtime directory

```
REPO=$(git rev-parse --show-toplevel)
AR_RUNTIME="$REPO/ar-runtime"
ENGINE="$AR_RUNTIME/scripts/ar-workflow-engine.py"
```

Engine, preflight, and monitor commands use cwd `$AR_RUNTIME`.

## Hard constraints

**Do not:**

- `web_search` / `web_fetch` / read papers or long logs yourself
- Run experiment code — that is ar-runner via `execute-run`
- Write plan.md or research code
- Create, overwrite, or patch `review.md`, `critic.md`, `blind_review.md`, `results/summary.md`, `results/run_receipts/`, or `results/run_artifacts/`
- Expand project permissions or write `<project_root>/.claude/settings.json`
- Edit `workflow_queue.json`, the engine mirror, or `workflow_events.jsonl`

**Do:**

- Read/write `state.md` and append `decisions.log`
- Dispatch via `spawn_subagent` / `resume_from` / claim workers
- Start/stop `scripts/ar-gemini-monitor.py`
- Close units only with engine commands (`complete` / `after-*`), always `--worker` in claim mode
- Output `<promise>AUTORESEARCH_DONE</promise>` only when the engine has closed

## Input

`$ARGUMENTS` = `<idea_file> [project_root]`. Resume from `state.md` / `workflow_queue.json` on the same `project_root`.

```
/ar-coordinator examples/ideas/synthetic_gpu_smoke.md
/ar-coordinator examples/ideas/synthetic_gpu_smoke.md data/projects/gpu_smoke
```

## Phase 0 — Parse and init

1. Split args. `idea_file` is first.
2. `python "$REPO/src/idea_provenance.py" inspect --idea-file "<idea_file>"` — non-zero **STOP**. Do not guess knowledge direction.
3. Default `project_root`: `$REPO/data/projects/<first 40 chars of idea_preview slug>`
4. First output:
   ```
   idea_file    = <resolved absolute path>
   idea         = <first 60 chars of idea_preview>
   project_root = <...>
   exists       = yes/no
   slug         = <project slug>
   ```
5. `./scripts/ar-preflight-mcp.sh` from `$AR_RUNTIME` — do not append `; echo`. Non-zero **STOP**.
6. `python "$REPO/src/idea_provenance.py" prepare --idea-file "<idea_file>" --project-root "<project_root>"`. Read `<project_root>/idea.md` as `idea_text`.
7. Skeleton (do not overwrite): `idea.md`, `idea_provenance.json`, `plan.md`, `state.md`, `code/`, `review.md`, `results/{run.log,notifications.log,monitor_state.json}`, `workflow_queue.json`, `decisions.log`.
8. Start the monitor unless `AR_SUPERVISOR_MONITOR=1` (`run_terminal_command` `background: true`). Start failure **STOP**.
9. `python "$ENGINE" init --project-root "<project_root>" --max-cycles "${AR_MAX_CYCLES:-3}"`. Failure **STOP**. Do not hand-write the queue.

No user `go` gate. Enter the pool loop.

## Pool loop

Every wave:

1. `python "$ENGINE" ready --project-root "<project_root>"`
2. Parse JSON `width` and `ready[]`.
3. If `width=0` and pending=running=0, go to Close.
4. Spawn **one worker per ready unit**, up to available agents (default panel 8, hard ceiling 32). Same-turn parallel `spawn_subagent`.
5. Each worker claims with a unique `--worker grok-w<wave>-<i>`:
   ```
   python "$ENGINE" claim --project-root "<project_root>" --worker <id> --prompt
   ```
   then executes that prompt and closes with the engine command **including `--worker`**.
6. Await the whole panel. Then start the next wave — newly unblocked units show up on `ready`.

Do not use `next-prompt` while a claim pool is running (`next-prompt` marks a unit running without a lease and steals from the pool).

### Unit type → work

| Engine `type` | Worker does |
|---|---|
| `agent` (`spawn_agents`) | `complete --status done --worker` |
| `planning` | `ar-planner` (or the claim worker writes plan.md), then `ar-gemini-reviewer` `plan_gate` |
| `coding` | `ar-coder`. Fan out every `subcoder_requests` entry in the same turn (ceiling 16). Then `code_gate` |
| `review` | `ar-gemini-reviewer` `code_review` with `unit`/`cycle`. You never write review.md |
| `run` | `ar-runner` + `execute-run`. Receipt must exist before `complete --status done` |
| `result-analysis` | Write findings after this claim, then `after-result-analysis --worker`. Do not `complete` this type |
| `critic` | `ar-critic`, then `after-critic --worker` |
| `blind-review` | `ar-blind-reviewer`, then `after-blind-review --worker` |
| `close` | Verify critic/blind-review, stop monitor, `complete` close |

Planner/coder/runner ids in `state.md` `## agents`. Resume with `resume_from` when width is 1 and the same role repeats.

### Run unit extra contract

A run unit may only execute its own stage. It must produce `execution_event_hash` via `execute-run`, write `results/run_receipts/<unit>.json`, and list every regular file under `results/run_artifacts/<unit>/`.

## Two-phase protocol

Phase 1 pilot then Phase 2 main, unless the idea is tiny/sanity-only. First planner: `mode=draft`, `phase=1`. Skip Phase 2 only with `phase_2_skipped_reason`. Scale-up planner: `mode=scale_up`.

## Isolation

All writes stay in `<project_root>`. External idea paths are read-only. Copy or clone into `code/vendor/` or `third_party/` before editing.

## Isomorphic fan-out

Independent seeds/ablations: one `spawn_subagent` (or workflow `parallel()` slot) per item in the same turn. Ceiling 32. Each run still sets `CUDA_VISIBLE_DEVICES` to the cards it will use — that is allocation, not a pool-size cap. Prefer `/ar-experiment-matrix` when the matrix is the whole job. Results still go through result-analysis → critic → blind-review.

## state.md

Include `idea_file`, `idea_artifact: <project_root>/idea.md`, `idea_provenance: <project_root>/idea_provenance.json`, `project_root`, `slug`, `## agents`, `## engine` (current ready width, worker ids, queue counts), `## findings`, `## artifacts`, `## recent_events`. No long logs.

## Close

Queue done, run gate approved, no unhandled `next_focus` / critic `required_next_focus`, Phase 2 analysis+critic or legal skip, blind-review complete, no pending experiment processes. Stop the monitor. Last line:

```xml
<promise>AUTORESEARCH_DONE</promise>
```
