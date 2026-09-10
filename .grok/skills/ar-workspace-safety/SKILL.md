---
name: ar-workspace-safety
description: >
  Hard filesystem and Python-environment safety rules for AutoResearch on a
  GPU server. MUST be applied for ANY write, delete, move, install, or python
  invocation. Triggers: write file, save, mkdir, rm, delete, move, mv, cp,
  overwrite, cleanup, git reset, git clean, pip install, conda create, python3,
  miniconda, env, sudo, .ssh, .env, secret, credentials. Independent of
  ar-experiment-runner. Use when the user runs /ar-workspace-safety.
---

# AR Workspace Safety

Single source of truth for: where can I write/delete, and how do I run Python/install packages.

Grok: `run_terminal_command` for shell; never `sudo`. Apply even for "just a quick install" or "just delete this".

## Default paths

```
AR_RUNTIME=<repo>/ar-runtime
DATA_DISK=.
WORKSPACE=$DATA_DISK/workspace
```

If `$DATA_DISK` is unclear, ask once before any destructive action.

## Writable paths

| Path | Use |
|---|---|
| `$AR_RUNTIME/` docs and `.grok/` skill source | Project docs |
| `$DATA_DISK/workspace/projects/<name>/` | Experiment code |
| `$DATA_DISK/workspace/artifacts/<name>/<run_id>/` | Logs, outputs, checkpoints |
| `$DATA_DISK/workspace/scratch/` | Temporary files |
| `$DATA_DISK/workspace/envs/` | Miniconda + per-project envs |
| AutoResearch `<project_root>/` including `.venv`, `code/`, `results/` | Coordinator-driven runs |
| `/tmp/` | Installer downloads only, then delete |

```
$DATA_DISK/workspace/
├── projects/
├── artifacts/
├── scratch/
└── envs/
    ├── .miniconda/
    └── <env_name>/
```

```bash
mkdir -p "$DATA_DISK/workspace"/{projects,artifacts,scratch,envs}
```

## Forbidden (no write, no delete)

- `/`, `/etc`, `/usr`, `/var`, `/opt`, `/boot`
- `~/.ssh/**`, `authorized_keys`, `sshd_config`, any private key
- `.git/` internals
- other users' homes
- shared datasets unless the user says they are writable
- `.env`, credential files, API key files
- system Python and `/usr/bin/python*`

## Hard rules

1. NEVER `rm -rf` outside `$DATA_DISK/workspace/` or the current AutoResearch `<project_root>/`.
2. NEVER `git reset --hard`, `git clean -fdx`, or broad `git checkout` unless the user asks AND you list what will be lost.
3. NEVER read, print, copy, or commit private keys or API secrets.
4. Before deleting more than one file, list every target and ask.
5. Before deleting a conda env, `pgrep -af "$DATA_DISK/workspace/envs/<env_name>/bin/python" || true`
6. NEVER `sudo`.
7. NEVER `conda init` or edit `~/.bashrc`. Activate inline.
8. NEVER use system `python3` for experiment code.
9. NEVER naked `pip install <pkg>`. Install into the profile env or `<project_root>/.venv` only.
10. If the user says "clean up", propose specific paths and wait.

## Python / conda

Coordinator-driven AutoResearch projects use `<project_root>/.venv` (see ar-runner). Standalone skill experiments use workspace-local Miniconda.

### Workspace Miniconda (one-time)

```bash
mkdir -p "$DATA_DISK/workspace/envs"
curl -fsSL -o /tmp/miniconda.sh https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash /tmp/miniconda.sh -b -p "$DATA_DISK/workspace/envs/.miniconda"
rm /tmp/miniconda.sh
```

Path-based envs, never `-n`:

```bash
"$DATA_DISK/workspace/envs/.miniconda/bin/conda" create \
  -p "$DATA_DISK/workspace/envs/<env_name>" python=3.11 -y
```

Activate inline before every Python or pip call. `sys.executable` MUST end with `<env_name>/bin/python` (or `<project_root>/.venv/bin/python`).

### What not to do

```bash
python3 script.py                    # system Python
pip install numpy                    # naked install
conda create -n <name> python=3.11   # name-based env
conda activate base
sudo apt install python3-...
echo 'conda activate ...' >> ~/.bashrc
```

## Cleanup

Propose specific paths first (`find` `__pycache__`, `du -sh` artifacts/envs). Wait for confirmation. Narrowest glob.

## API keys

Environment variables only. Never write secrets into README, YAML, JSON, logs, scripts, or chat.
