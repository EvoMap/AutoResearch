---
name: ar-gpu-preflight
description: >
  GPU need analysis, sizing, allocation, and pre-run RED/YELLOW/GREEN check.
  MUST be used before running any code that imports torch/jax/tensorflow,
  mentions cuda/GPU, uses .cuda() / .to("cuda") / device="cuda", may allocate
  GPU memory, runs longer than 30s, or trains/infers a model. Triggers:
  nvidia-smi, CUDA, torch.cuda, GPU, VRAM, OOM, CUDA_VISIBLE_DEVICES, smoke
  test, preflight. Independent of ar-experiment-runner. Use when the user
  runs /ar-gpu-preflight.
---

# AR GPU Preflight

Before any code runs: does this need GPU, how much, and is the box safe (RED/YELLOW/GREEN)?

Grok: run probes with `run_terminal_command` locally — do not wrap in SSH.

## When

Always, before `python <script>` or `bash run.sh` if it may take > 30s, anything importing torch/jax/tensorflow, anything with `cuda` / `.cuda()` / `device="cuda"`, training or inference, large downloads or checkpoint writes.

Skip only if the user says "skip preflight".

## GPU need

Read the code first.

GPU-needed: `torch.cuda`, `.cuda()`, `.to("cuda")`, `device="cuda"`, JAX/TF GPU, non-trivial train/infer, explicit GPU request.

CPU-only: numpy/pandas/sklearn without GPU backends; small file processing.

Never silently downgrade GPU code to CPU because `torch.cuda.is_available()` is False.

## Sizing

float32 = 4 B, float16/bfloat16 = 2 B. For `a @ b` count inputs + output. Add ~0.5–1 GiB framework overhead. If unsure, mark YELLOW and pick a card with generous free memory.

## Live GPU state

```bash
nvidia-smi -L
nvidia-smi
nvidia-smi --query-gpu=index,name,memory.free,memory.total,utilization.gpu --format=csv
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```

If `nvidia-smi` fails, stop. Do not install drivers.

CUDA check from the **profile env or project venv**, never system Python:

```bash
<env>/bin/python -c 'import torch; print(torch.cuda.is_available(), torch.cuda.device_count())'
```

## Card allocation

Always set `CUDA_VISIBLE_DEVICES` explicitly. Never let a job see every GPU by default. Multi-GPU only after proving the code uses them (DDP, model parallel).

## Preflight table

```
Preflight: <project>/<script>
Profile:    <profile>
Workspace:  ...
Env:        ...
Artifacts:  ...

GPU need:                 required / optional / none / unknown
Estimated cards:          <N or unknown>
Estimated memory/card:    <GB or unknown>
Available cards:          <id:free GiB list>
Chosen CUDA_VISIBLE_DEVICES: <ids or "">

A1 env activates           GREEN/YELLOW/RED
A2 imports resolve         GREEN/YELLOW/RED
B0 GPU need classified     GREEN/YELLOW/RED/N/A
B1 CUDA reachable          GREEN/YELLOW/RED/N/A
B2 GPU free enough         GREEN/YELLOW/RED/N/A
B3 card allocation plan    GREEN/YELLOW/RED/N/A
C1 CPU/RAM state           GREEN/YELLOW/RED
C2 disk space              GREEN/YELLOW/RED
D1 output paths            GREEN/YELLOW/RED
D2 destructive ops         GREEN/YELLOW/RED
D3 seed/reproducibility    GREEN/YELLOW/RED
E1 runtime command         GREEN/YELLOW/RED

VERDICT: GREEN / YELLOW / RED — <short reason>
Recommended command:
  <full command, env-activated, CUDA_VISIBLE_DEVICES, log redirected>
```

C1/C2: `nproc; uptime; free -h; df -h "$DATA_DISK"`. Disk: GREEN > 10 GB, YELLOW 2–10 GB, RED < 2 GB.

## Verdict

- Any RED → stop. Do not run.
- Any YELLOW (no RED) → explain and ask, unless the user already said proceed on YELLOW.
- All GREEN/N/A → show the table, then run is allowed.

GPU-needed automatic RED: `nvidia-smi` not run, no memory/card estimate, no `CUDA_VISIBLE_DEVICES`, chosen cards busy with insufficient free memory.

## Hard rules

1. NEVER run CUDA without checking `nvidia-smi` free memory and processes.
2. NEVER let a GPU job grab every GPU by default.
3. NEVER fix `torch.cuda.is_available() == False` by installing drivers first.
4. NEVER run the CUDA check with bare `python -c`.
5. NEVER report GREEN without running the probes.
6. NEVER silently substitute CPU for a GPU job.
