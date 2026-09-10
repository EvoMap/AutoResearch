---
name: ar-experiment-runner
description: >
  Top-level short-experiment orchestrator on a Linux GPU server. Use for
  "I need to do an experiment", "run this experiment", "smoke test",
  "train a model", or any request to write/run experiment code outside the
  full AutoResearch coordinator pipeline. Coordinates profile inference,
  workspace, env, GPU preflight, execution, and artifacts. Always co-applies
  ar-gpu-preflight and ar-workspace-safety. Use when the user runs
  /ar-experiment-runner.
---

# AR Experiment Runner

End-to-end controller for a one-off experiment. For a full idea → plan → review → critic pipeline, use `/ar-coordinator` or the `ar-coordinator` workflow instead.

Always apply `ar-gpu-preflight` and `ar-workspace-safety`. If they are not loaded, follow their rules anyway.

Grok: `run_terminal_command` for shell (background for jobs > ~5 min), `write` for code. Do not spawn nested experiment agents unless independent isomorphic items need `ar-experiment-matrix`.

## Default paths

```
DATA_DISK=.
WORKSPACE=$DATA_DISK/workspace
```

If `$DATA_DISK` does not exist, inspect `pwd` / `df -h` and ask once.

## Phase 0 — Profile from minimal input

1. Restate the inferred goal in one sentence.
2. Slug: lowercase, hyphens, ≤ 64 chars.
3. Paths:
   ```
   profile   = <slug>
   workspace = $DATA_DISK/workspace/projects/<slug>
   env       = $DATA_DISK/workspace/envs/<slug>
   artifacts = $DATA_DISK/workspace/artifacts/<slug>/<run_id>
   run_id    = $(date +%Y%m%dT%H%M)-<slug>
   ```
4. If `config/experiment-profiles*.json` defines `<slug>`, use that file.
5. If the goal is too vague, ask at most 1–3 focused questions.

## Phase 1 — Plan

Print a short plan. Wait for `go` only if destructive, expensive, or long. Routine smoke tests proceed.

```
Profile / Workspace / Env / Artifacts:
GPU need: required / optional / none / unknown
Planned code files / deps:
```

## Phase 2 — Workspace

```bash
mkdir -p "$DATA_DISK/workspace"/{projects,artifacts,scratch,envs}
mkdir -p "$DATA_DISK/workspace/projects/<slug>"
mkdir -p "$DATA_DISK/workspace/artifacts/<slug>/<run_id>"
```

Never write outside `$DATA_DISK/workspace/` except `/tmp/` for transient downloads.

## Phase 3 — Code

Write under `$DATA_DISK/workspace/projects/<slug>/` with a clear entrypoint. Configurable output paths defaulting to artifacts. Deterministic seed when there is randomness. No hardcoded paths outside the workspace.

If the user gave an existing script, read it first; do not run until env + preflight are ready.

## Phase 4 — Env

Workspace-local Miniconda only (`$DATA_DISK/workspace/envs/.miniconda/`). Path-based env, never `-n`. Always install/run via that env. NEVER `python3 <script>`, naked `pip install`, conda `base`, or `conda init`.

## Phase 5 — GPU preflight

Emit the `ar-gpu-preflight` table. RED stops; YELLOW asks; GREEN runs. GPU code missing `nvidia-smi`, memory estimate, or `CUDA_VISIBLE_DEVICES` is RED.

## Phase 6 — Run

Short foreground: activate env, `CUDA_VISIBLE_DEVICES=<ids>`, python entrypoint, log to artifacts.

Long job (>~5 min or training): `run_terminal_command` with `background: true` (or nohup/tmux), capture PID, log to artifacts.

## Phase 7 — Report

Profile, paths, GPU allocation, command, exit code/PID, log path. Tail short-run logs; for background jobs give PID and log path.

## Hard rules

1. NEVER run experiment code with system `python3` or conda `base`.
2. NEVER naked `pip install`.
3. NEVER touch GPU code paths without the preflight table.
4. NEVER write outside `$DATA_DISK/workspace/` (except `/tmp/` downloads).
5. NEVER `rm -rf`, `git reset --hard`, or bulk-delete without listing targets and asking.
6. NEVER ask the user to paste a long checklist when minimal input is enough.
7. NEVER let a GPU job grab every card — `CUDA_VISIBLE_DEVICES` is mandatory.
