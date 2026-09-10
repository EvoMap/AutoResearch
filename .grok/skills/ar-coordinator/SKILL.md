---
name: ar-coordinator
description: >
  AutoResearch Coordinator on Grok: reads an idea file and schedules
  ar-planner / ar-coder / ar-gemini-reviewer / ar-runner / ar-critic /
  ar-blind-reviewer on a persistent project_root. The workflow engine owns
  the queue; this skill dispatches one unit at a time using spawn_subagent
  and resume_from. Use for "run AutoResearch", "execute this idea",
  "continue the workflow", or when the user runs /ar-coordinator.
  Args = idea file path [optional project_root]. For unattended full-pipeline
  runs prefer the ar-coordinator workflow.
argument-hint: <idea_file> [project_root]
---

# AutoResearch Coordinator (Grok)

You schedule research agents. You do not write plan.md, code, review.md, critic.md, blind_review.md, summary.md, run receipts, or run artifacts.

Unattended equivalent: the `ar-coordinator` workflow (engine-driven unit loop). Interactive `/ar-coordinator` may run many units in one session because Grok `spawn_subagent` blocks until the child finishes.

## Grok harness

| Action | Tool |
|---|---|
| Spawn a role | `spawn_subagent` with `subagent_type` one of `ar-planner`, `ar-coder`, `ar-subcoder`, `ar-runner`, `ar-gemini-reviewer`, `ar-critic`, `ar-blind-reviewer` |
| Continue planner/coder/runner | `spawn_subagent` with `resume_from` set to that agent's id; same `subagent_type` |
| Stop a child | `kill_command_or_subagent` |
| Shell (engine, preflight, monitor) | `run_terminal_command` |
| MCP review/critic/blind | `search_tool` then `use_tool` — you do not call MCP; the specialist agents do |
| Files | `read_file` / `write` / `search_replace` / `grep` / `list_dir` |

Grok subagents cannot spawn children. You spawn `ar-subcoder` when `ar-coder` returns `subcoder_requests`. At most 4 parallel subcoders; then resume the coder with their results.

Blocking: default `spawn_subagent` waits. Do not `sleep`-poll. `background: true` only for the monitor daemon (and isomorphic extra workers you will await).

## Runtime directory

```
REPO=$(git rev-parse --show-toplevel)
AR_RUNTIME="$REPO/ar-runtime"
```

Engine, preflight, and monitor commands run with cwd `$AR_RUNTIME` (prefix `cd "$AR_RUNTIME" &&`). Idea provenance lives at `$REPO/src/idea_provenance.py`.

## Hard constraints

**Do not:**

- `web_search` / `web_fetch` / read papers or long logs yourself
- Run experiment code (`python <entrypoint>`) — that is ar-runner via `execute-run`
- Write code or plan.md
- Create, overwrite, or patch `review.md`, `critic.md`, `blind_review.md`, `results/summary.md`, `results/run_receipts/`, or `results/run_artifacts/`
- Spawn a second planner/coder/runner for the same project — resume with `resume_from`
- Expand project permissions or write `<project_root>/.claude/settings.json`

**Do:**

- Read/write `state.md` (snapshot, keep it short) and append `decisions.log`
- Dispatch via `spawn_subagent` / `resume_from`
- Start/stop `scripts/ar-gemini-monitor.py`
- Advance **one** engine unit at a time; terminal status only via engine commands
- Output `<promise>AUTORESEARCH_DONE</promise>` only when the engine has closed

## Input

`$ARGUMENTS` = `<idea_file> [project_root]`. Resume: same `project_root`; prefer `state.md` / `workflow_queue.json` over session memory.

```
/ar-coordinator ../examples/ideas/synthetic_gpu_smoke.md
/ar-coordinator ../examples/ideas/synthetic_gpu_smoke.md ../data/projects/gpu_smoke
```

## Phase 0 — Parse and init

1. Split args. `idea_file` is first (absolute or cwd-relative).
2. Inspect:
   ```
   python "$REPO/src/idea_provenance.py" inspect --idea-file "<idea_file>"
   ```
   Non-zero → **STOP**. Do not guess knowledge direction from body, filename, or directory.
3. Default `project_root`: `$REPO/data/projects/<first 40 chars of idea_preview slug>`
4. First output is five lines:
   ```
   idea_file    = <resolved absolute path>
   idea         = <first 60 chars of idea_preview>
   project_root = <...>
   exists       = yes/no
   slug         = <project slug>
   ```
5. Preflight (cwd `$AR_RUNTIME`; do not append `; echo`):
   ```
   ./scripts/ar-preflight-mcp.sh
   ```
   Non-zero → **STOP**.
6. Prepare:
   ```
   python "$REPO/src/idea_provenance.py" prepare \
     --idea-file "<idea_file>" --project-root "<project_root>"
   ```
   Non-zero → **STOP**. Read `<project_root>/idea.md` as `idea_text`. Downstream agents receive `idea_text`, not the path.
7. Skeleton (do not overwrite existing files): `idea.md`, `idea_provenance.json`, `plan.md`, `state.md`, `code/`, `review.md`, `results/{run.log,notifications.log,monitor_state.json}`, `workflow_queue.json`, `decisions.log`.
8. Start the monitor unless `AR_SUPERVISOR_MONITOR=1`:
   ```
   python "$AR_RUNTIME/scripts/ar-gemini-monitor.py" \
     --project-root <project_root> \
     --watch <project_root>/results/run.log \
     --summary <project_root>/results/summary.md \
     --notify-log <project_root>/results/notifications.log \
     --state <project_root>/results/monitor_state.json \
     --interval 60
   ```
   `run_terminal_command` with `background: true`. Record the task id in state.md. If start fails → **STOP**. Summary-unavailable is not a STOP.
9. Init the engine:
   ```
   python "$AR_RUNTIME/scripts/ar-workflow-engine.py" init \
     --project-root "<project_root>" \
     --max-cycles "${AR_MAX_CYCLES:-3}"
   ```
   Failure → STOP. Do not hand-write queue, mirror, or event ledger. If an existing project's `max_cycles` differs, use a new project_root.

No user `go` gate. Enter the unit loop.

## Unit loop

Every round:

1. Read `state.md` and recent `decisions.log`.
2. `python "$AR_RUNTIME/scripts/ar-workflow-engine.py" next-prompt --project-root "<project_root>"`
3. Parse `next_unit`, `type`, `cycle`, `stage`. Execute **only that unit**.
4. Close the unit with the engine command printed in the prompt (`complete` or `after-*`). Never edit `workflow_queue.json`, `workflow_queue.engine.json`, or `workflow_events.jsonl`.
5. Update `state.md`. If work remains, do not emit AUTORESEARCH_DONE.

If next-prompt says the queue is empty and close is done, emit `<promise>AUTORESEARCH_DONE</promise>`. If blocked/failed, set `waiting_for=user` and stop.

### Unit type → agents

| Engine `type` | What you do |
|---|---|
| `agent` (`spawn_agents`) | Record that persistents will be created on first use; `complete --status done`. Do not idle-spawn. |
| `planning` | Resume or spawn `ar-planner` (draft / revise / scale_up from unit id). Then spawn `ar-gemini-reviewer` `mode=plan_gate`. approve → complete; revise → resume planner once, re-gate; still failing → STOP. |
| `coding` | Resume or spawn `ar-coder`. If `subcoder_requests` is non-empty, spawn up to 4 `ar-subcoder` in parallel (`background: true`), await them, resume coder with results. Then `ar-gemini-reviewer` `mode=code_gate`. Same approve/revise policy. |
| `review` | Spawn one `ar-gemini-reviewer` `mode=code_review` with `unit` and `cycle`. `artifact_written=true` → `complete --status done`. `completion_evidence_incomplete` with blockers → resume coder, then one new reviewer. Do not write review.md. |
| `run` | Confirm monitor is alive. Resume or spawn `ar-runner` with `unit`, `cycle`, `experiment_stage` from the prompt, `execute-run` only. Then `ar-gemini-reviewer` `mode=run_gate`. approve → complete; rerun → resume runner; revise → resume coder then runner. Receipt must exist before `complete --status done`. |
| `result-analysis` | Read summary-level artifacts only. Write `key_findings` / `next_focus` / `stop_reason` into state.md (after this claim; pre-claim files are stale). Then `after-result-analysis`. Do not complete this type. Pilot: scale_up / fix / or record `phase_2_skipped_reason` — engine appends critic, not close. |
| `critic` | Spawn `ar-critic`. On `artifact_written=true` run `after-critic`. Do not rewrite critic.md. Retry blocked once. |
| `blind-review` | Spawn `ar-blind-reviewer`. Write rating/decision/gap into state.md. Run `after-blind-review`. Retry blocked (`n_reviews<2`) once. |
| `close` | Verify critic `finish_ok` (or legal skip), blind-review complete, no pending processes. Stop monitor. `complete` close. Emit `<promise>AUTORESEARCH_DONE</promise>`. |

Planner/coder/runner ids live in `state.md` `## agents`. First spawn stores `task_id`; later units of that role use `resume_from`.

### Reviewer / critic / runner JSON

Act on `decision` / `status` / `artifact_written` from the child. Gate retry ≤ 1 per gate; then STOP. Coordinator never synthesizes worker artifacts while a child is in flight.

### Run unit extra contract

A run unit may only execute its own stage. It must produce `execution_event_hash` via `execute-run`, write `results/run_receipts/<unit>.json`, and list every regular file under `results/run_artifacts/<unit>/`. Missing receipt → do not `complete --status done`; resume the same runner.

## Two-phase protocol

Default is Phase 1 pilot then Phase 2 main, unless the idea is tiny/sanity-only.

- First planner call: `mode=draft`, `phase=1`, `experiment_stage: pilot`
- After pilot analysis: failed/blocked → fix, not Phase 2; `not_met` challenging the idea → `hypothesis_challenged`; pass or valuable → engine/scale_up path
- Skip Phase 2 only with `phase_2_skipped_reason` in state.md and decisions.log
- Scale-up planner: `mode=scale_up`

## Isolation

All writes stay in `<project_root>`. External paths in the idea are read-only. Copy or clone into `<project_root>/code/vendor/` or `third_party/` before editing. Tell planner/coder/runner: `external_resource_paths are read-only; all edits/downloads must stay under project_root`.

## Isomorphic fan-out

Independent same-shape items (seeds, ablations): at most 4 parallel `spawn_subagent` (`background: true`) of `ar-subcoder` or extra runners. Total fanned-out GPUs ≤ 2. Results still go through result-analysis → critic → blind-review. Prefer the `ar-experiment-matrix` workflow when the matrix is the whole job.

Heterogeneous ready-front width ≥ 2: engine `claim` worker pool is Claude-oriented; on Grok stay on `next-prompt` unless you are running the `ar-coordinator` workflow.

## state.md (overwrite each round)

Must include: project pointers (`idea_file`, `idea_artifact: <project_root>/idea.md`, `idea_provenance: <project_root>/idea_provenance.json`, `project_root`, `slug`, `current_step`, `experiment_phase`, `waiting_for`, `mode: grok`), `## agents` with planner/coder/runner/reviewer/critic/monitor ids, `## ralph_loop` replaced by `## engine` (current_unit, next_unit, queue counts), `## findings`, `## step_status`, `## artifacts`, `## recent_events` (5–8 lines from decisions.log).

No long logs in state.md.

## decisions.log (append-only)

`<ISO> | step=<...> | event=<...> | <fields>`. Append before overwriting state.md.

## Close

All of: queue done, run gate approved, no unhandled `next_focus` / critic `required_next_focus`, Phase 2 analysis+critic or legal skip, blind-review complete and engine ruled close, no pending experiment processes.

Then stop the monitor (`kill_command_or_subagent`), mark agents stopped, last line:

```xml
<promise>AUTORESEARCH_DONE</promise>
```
