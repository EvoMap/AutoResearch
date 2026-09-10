---
name: ar-experiment-runner
description: ar-runtime skills top-level experiment orchestrator on a Linux GPU server. MUST be used for any short experiment request such as "I need to do an experiment", "I want to do an experiment", "help me run an experiment", "do this experiment", "run this experiment", "run this piece of code", "run an experiment", "create and run an experiment", "smoke test", "train a model", or any request to write/run experiment code. Coordinates profile inference, workspace creation, env setup, GPU preflight, execution, and artifacts in a single skill. Always co-applies ar-gpu-preflight and ar-workspace-safety.
---

# AR Experiment Runner

End-to-end controller for ar-runtime skills experiments. The user should be able to say only:

```text
I need to do a <experiment_goal> experiment
```

and this skill drives everything. Two sibling skills are always in force and MUST be respected even mid-flow:

- `ar-gpu-preflight` — GPU need / sizing / RED-YELLOW-GREEN
- `ar-workspace-safety` — filesystem safety + correct conda/pip usage

If either is not loaded in this session, follow the rules below as if they were.

## Default Paths

```text
DATA_DISK=.
WORKSPACE=$DATA_DISK/workspace
```

If `$DATA_DISK` does not exist, inspect `pwd` / `df -h` and ask once before continuing.

## Phase 0 — Resolve Profile From Minimal Input

For "I need to do a <goal> experiment" style input:

1. Restate the inferred goal in one sentence.
2. Build a slug (lowercase, hyphens, ≤ 64 chars). Examples:
   - "GPU smoke test" → `gpu-smoke`
   - "CPU matmul smoke" → `cpu-matmul-smoke`
   - "train mnist classifier" → `train-mnist-classifier`
3. Derive paths:

   ```text
   profile   = <slug>
   workspace = $DATA_DISK/workspace/projects/<slug>
   env       = $DATA_DISK/workspace/envs/<slug>
   artifacts = $DATA_DISK/workspace/artifacts/<slug>/<run_id>
   run_id    = $(date +%Y%m%dT%H%M)-<slug>
   ```

4. If `config/experiment-profiles*.json` defines `<slug>`, use that file's paths/deps/run instead.
5. If the goal is too vague to infer GPU need or code shape, ask AT MOST 1–3 focused questions. Never ask the user to paste a long checklist.

If the user gave an existing script path under `workspace/projects/<name>/`, infer profile = `<name>` and read the script before doing anything else.

## Phase 1 — Plan

Print a short plan and wait for `go` only if the operation is destructive, expensive, or long. For routine smoke tests, proceed without confirmation.

```text
Profile:
Workspace:
Env:
Artifacts:
GPU need: required / optional / none / unknown
Planned code files:
Planned deps:
```

## Phase 2 — Workspace

```bash
mkdir -p "$DATA_DISK/workspace"/{projects,artifacts,scratch,envs}
mkdir -p "$DATA_DISK/workspace/projects/<slug>"
mkdir -p "$DATA_DISK/workspace/artifacts/<slug>/<run_id>"
```

Never write outside `$DATA_DISK/workspace/`. The only allowed exception is `/tmp/` for installer downloads that you delete afterwards. (Full rules: `ar-workspace-safety`.)

## Phase 3 — Code

If you are writing the experiment:

- Put code under `$DATA_DISK/workspace/projects/<slug>/`.
- Use a clear entrypoint: `run.py`, `train.py`, `matmul.py`, `gpu.py`, etc.
- Make output paths configurable; default them under the artifacts dir.
- Set a deterministic seed when there is randomness.
- No hardcoded paths outside the workspace.

If the user provided an existing script:

- Read it first.
- Do not run until env + preflight are ready.

## Phase 4 — Env (delegate to ar-workspace-safety)

Use the workspace-local Miniconda only:

```text
$DATA_DISK/workspace/envs/.miniconda/
```

Create or reuse the profile env by **path** (never `-n <name>`):

```bash
"$DATA_DISK/workspace/envs/.miniconda/bin/conda" create \
  -p "$DATA_DISK/workspace/envs/<slug>" python=3.11 -y
```

Always install and run via the activated profile env:

```bash
source "$DATA_DISK/workspace/envs/.miniconda/etc/profile.d/conda.sh" && \
conda activate "$DATA_DISK/workspace/envs/<slug>" && \
pip install <deps>
```

HARD RULES (also enforced by `ar-workspace-safety`):

- NEVER `python3 <script>` — always activate the profile env first, or call `<env>/bin/python` directly.
- NEVER naked `pip install <pkg>` to fix `ModuleNotFoundError`.
- NEVER use conda `base`, system Python, or `conda init` / `~/.bashrc` edits.

## Phase 5 — GPU + Preflight (delegate to ar-gpu-preflight)

Before any run, output the preflight table from `ar-gpu-preflight`. Minimum fields:

```text
Profile / Workspace / Env / Artifacts:
GPU need:
Estimated cards:           Estimated memory:
Available cards:           Chosen CUDA_VISIBLE_DEVICES:
A1 env activates / A2 imports resolve
B0 GPU need / B1 CUDA reachable / B2 GPU free / B3 card plan
C1 CPU/RAM / C2 disk
D1 output paths / D2 destructive ops / D3 seed
E1 runtime command
VERDICT: GREEN / YELLOW / RED — <reason>
```

Verdict rules: **RED stops; YELLOW asks; GREEN runs.** For GPU code, missing `nvidia-smi` check, missing memory estimate, or missing `CUDA_VISIBLE_DEVICES` is automatically RED.

## Phase 6 — Run

Short foreground run:

```bash
source "$DATA_DISK/workspace/envs/.miniconda/etc/profile.d/conda.sh" && \
conda activate "$DATA_DISK/workspace/envs/<slug>" && \
cd "$DATA_DISK/workspace/projects/<slug>" && \
CUDA_VISIBLE_DEVICES=<ids_if_gpu> python <entrypoint> \
  > "$DATA_DISK/workspace/artifacts/<slug>/<run_id>/run.log" 2>&1
```

Long job (>~5 min or training): use `nohup` or `tmux`, capture PID/session, log to artifacts:

```bash
mkdir -p "$DATA_DISK/workspace/artifacts/<slug>/<run_id>"
nohup bash -lc '
source "$DATA_DISK/workspace/envs/.miniconda/etc/profile.d/conda.sh" &&
conda activate "$DATA_DISK/workspace/envs/<slug>" &&
cd "$DATA_DISK/workspace/projects/<slug>" &&
CUDA_VISIBLE_DEVICES=<ids> python <entrypoint>
' > "$DATA_DISK/workspace/artifacts/<slug>/<run_id>/run.log" 2>&1 &
echo $!
```

## Phase 7 — Report

```text
Profile:
Workspace:
Env:
Artifacts:
GPU allocation:
Command:
Exit code / PID:
Key output or log path:
```

Tail the log for short runs; for background jobs, give the user the PID and log path.

## Hard Rules (Defense In Depth)

1. NEVER run user experiment code with system `python3` or conda `base`.
2. NEVER naked `pip install`. Install only into the activated profile env.
3. NEVER touch GPU code paths without showing the `ar-gpu-preflight` table first.
4. NEVER write outside `$DATA_DISK/workspace/` (except `/tmp/` for transient downloads).
5. NEVER `rm -rf`, `git reset --hard`, or bulk-delete without listing targets and asking.
6. NEVER ask the user to paste a long ar-runtime skills checklist when minimal input is enough.
7. NEVER let a GPU job grab every card by default — `CUDA_VISIBLE_DEVICES` is mandatory.

## Anti-Patterns

- Running first, deciding env/GPU afterwards.
- Fixing `ModuleNotFoundError` with `pip install <pkg>` outside the profile env.
- "It worked because base happened to be active."
- Silently rewriting a GPU task to CPU because `torch.cuda.is_available()` returned False.
- Skipping preflight on a "small" run that turns out to allocate a lot of GPU memory.
