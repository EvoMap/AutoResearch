---
name: ar-gpu-preflight
description: GPU need analysis, sizing, allocation, and pre-run RED/YELLOW/GREEN check on a Linux GPU server. MUST be used before running any code that imports torch/jax/tensorflow, mentions cuda/GPU, uses .cuda() / .to("cuda") / device="cuda", may allocate GPU memory, runs longer than 30s, or trains/infers a model. Triggers like nvidia-smi, CUDA, torch.cuda, GPU, 显存, 显卡, 跑代码, 运行代码, 跑一下, 训练, train, inference, smoke test, preflight, RED YELLOW GREEN, CUDA_VISIBLE_DEVICES, OOM. Independent of ar-experiment-runner — load and apply this skill even when the user gives a one-line GPU code request.
---

# AR GPU Preflight

Single source of truth for two questions before any code runs:

1. Does this run need GPU, and if so, how much?
2. Is the system in a state where running it now is safe (RED/YELLOW/GREEN)?

This skill is independent. Do not skip it because some other orchestrator "should" handle preflight.

## When To Run

Always, before:

- `python <script>` or `bash run.sh` if the script may take > 30s
- anything importing `torch`, `jax`, `tensorflow`
- anything containing `cuda`, `.cuda()`, `.to("cuda")`, `device="cuda"`
- training or inference of any model
- large downloads or checkpoint writes

Skip only if the user explicitly says "skip preflight".

## Decision 1 — GPU Need

Read the code or command first. Classify:

GPU-needed signals:

- `torch.cuda`, `.cuda()`, `.to("cuda")`, `device="cuda"`
- JAX/TF GPU use
- training / inference of a non-trivial model
- explicit user request for GPU

CPU-only signals:

- `numpy` / `pandas` / `sklearn` without GPU backends
- small smoke tests, file processing, plotting

Never silently downgrade GPU code to CPU because `torch.cuda.is_available()` is False — surface the problem, do not paper over it.

## Decision 2 — GPU Sizing

If GPU is needed, estimate before running:

- Tensors: `num_elements * bytes_per_element`. float32 = 4 B, float16/bfloat16 = 2 B.
- For `a @ b`, count at least `inputs + output`.
- Add framework overhead (PyTorch ~0.5–1 GiB minimum).
- If unsure, mark estimate as YELLOW and pick a card with generous free memory.

Worked example: two 8000×8000 float32 inputs + one output:

```text
3 * 8000 * 8000 * 4 bytes ≈ 0.72 GiB  → single-card smoke test
```

## Decision 3 — Live GPU State

Always run these probes (locally — do NOT wrap in SSH):

```bash
nvidia-smi -L
nvidia-smi
nvidia-smi --query-gpu=index,name,memory.free,memory.total,utilization.gpu --format=csv
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```

If `nvidia-smi` itself fails, stop. Report the driver/CUDA issue. Do not attempt to install drivers.

Then verify CUDA from inside the profile env (NOT system Python):

```bash
source "$DATA_DISK/workspace/envs/.miniconda/etc/profile.d/conda.sh" && \
conda activate "$DATA_DISK/workspace/envs/<env_name>" && \
python -c 'import torch; print(torch.cuda.is_available(), torch.cuda.device_count())'
```

## Decision 4 — Card Allocation

Always pick `CUDA_VISIBLE_DEVICES` explicitly. On a shared box, never let a job see all GPUs by default.

```bash
CUDA_VISIBLE_DEVICES=0 python train.py            # single-card
CUDA_VISIBLE_DEVICES=0,1 python train.py          # multi-card, only after proving the code uses them
CUDA_VISIBLE_DEVICES="" python run_cpu.py         # explicit CPU-only
```

For multi-GPU, justify in writing why the code can use multiple cards (DDP, model parallel, etc.) before selecting more than one.

## Preflight Output Format

Always emit this exact table (any column may be `N/A` for CPU-only):

```text
Preflight: <project>/<script>
Profile:    <profile>
Workspace:  $DATA_DISK/workspace/projects/<workspace_name>
Env:        $DATA_DISK/workspace/envs/<env_name>
Artifacts:  $DATA_DISK/workspace/artifacts/<workspace_name>/<run_id>

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
  <full command, env-activated, with CUDA_VISIBLE_DEVICES, log redirected>
```

Probes for C1/C2:

```bash
nproc
uptime
free -h
df -h "$DATA_DISK"
```

Disk thresholds: GREEN > 10 GB free, YELLOW 2–10 GB, RED < 2 GB.

## Verdict Rules

- **Any RED** → stop and ask. Do not run.
- **Any YELLOW** (no RED) → explain the risk and ask, unless the user already said to proceed on YELLOW.
- **All GREEN/N/A** → run is allowed after you show the table.

For GPU-needed code, ANY of the following is automatic RED:

- `nvidia-smi` not run
- no memory/card estimate
- no `CUDA_VISIBLE_DEVICES`
- chosen cards are visibly busy with insufficient free memory

## Hard Rules (Defense In Depth)

1. NEVER run a CUDA script without first checking `nvidia-smi` free memory and processes.
2. NEVER let a GPU job grab every GPU by default.
3. NEVER fix `torch.cuda.is_available() == False` by installing CUDA drivers as a first response.
4. NEVER run the CUDA check with bare `python -c` — always activate the profile env first.
5. NEVER report GREEN without actually running the probes.
6. NEVER silently substitute CPU for a GPU job.

## Anti-Patterns

- "It's a small script, skip preflight." — That's how OOMs happen on a shared GPU.
- Reporting B2 GREEN without `nvidia-smi --query-compute-apps`.
- Multi-GPU launch with no justification.
- Using `python -c 'import torch...'` without activating the env (hits system Python).
- Putting the run command inline in chat without log redirection to artifacts.
