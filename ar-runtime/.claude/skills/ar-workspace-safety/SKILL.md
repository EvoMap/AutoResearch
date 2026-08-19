---
name: ar-workspace-safety
description: Hard filesystem and Python-environment safety rules for ar-runtime skills on a Linux GPU server. MUST be applied for ANY write, delete, move, install, or python invocation. Triggers like write file, save, mkdir, rm, delete, move, mv, cp, overwrite, cleanup, git reset, git clean, pip install, conda create, conda install, python3, python, miniconda, env, sudo, .ssh, .env, secret, credentials, /etc, /usr, /var, /opt, 删除, 安装, 写入, 清理. Independent of ar-experiment-runner — load and apply this skill even when no experiment context is in scope.
---

# AR Workspace Safety

The single source of truth for two questions:

1. Where can I write/delete on this machine?
2. How do I run Python and install packages safely?

This skill is independent. Apply it even when the user asks for "just a quick install" or "just delete this".

## Default Paths

```text
AR_IN_CC=.
DATA_DISK=.
WORKSPACE=$DATA_DISK/workspace
```

If `$DATA_DISK` is unclear, ask once before any destructive action.

## Writable Paths

| Path | Use |
|---|---|
| `$AR_IN_CC/README.md`, `quickstart.md`, `docs/`, `claude-skills/` | Project docs and skill source |
| `$DATA_DISK/workspace/projects/<workspace_name>/` | Experiment code |
| `$DATA_DISK/workspace/artifacts/<workspace_name>/<run_id>/` | Logs, outputs, checkpoints |
| `$DATA_DISK/workspace/scratch/` | Temporary files |
| `$DATA_DISK/workspace/envs/` | Miniconda + per-project envs |
| `~/.claude/skills/` | Installed skill copies |
| `/tmp/` | Installer downloads only, then delete |

Top-level layout under `workspace/`:

```text
$DATA_DISK/workspace/
├── projects/
├── artifacts/
├── scratch/
└── envs/
    ├── .miniconda/
    └── <env_name>/
```

Initialize idempotently:

```bash
mkdir -p "$DATA_DISK/workspace"/{projects,artifacts,scratch,envs}
```

## Forbidden Paths (No Write, No Delete)

- `/`, `/etc`, `/usr`, `/var`, `/opt`, `/boot`
- `~/.ssh/**`, `authorized_keys`, `sshd_config`, any private key
- `.git/` internals
- other users' home directories
- shared dataset directories unless the user explicitly says they are writable
- `.env`, credential files, API key files
- system Python and `/usr/bin/python*`

## Hard Rules

1. NEVER `rm -rf` outside `$DATA_DISK/workspace/`.
2. NEVER `git reset --hard`, `git clean -fdx`, or broad `git checkout` unless the user explicitly asks AND you list what will be lost.
3. NEVER read, print, copy, or commit private keys or API secrets.
4. Before deleting more than one file, list every target and ask.
5. Before deleting a conda env, verify nothing is using it:
   ```bash
   pgrep -af "$DATA_DISK/workspace/envs/<env_name>/bin/python" || true
   ```
6. NEVER `sudo` for ar-runtime skills work.
7. NEVER `conda init` or edit `~/.bashrc`. Always activate inline.
8. NEVER use system `python3` for experiment code.
9. NEVER naked `pip install <pkg>` to fix a missing import. Install into the profile env only.
10. If the user says "clean up", propose specific paths and wait for confirmation.

## Correct Python And Conda Usage

This is the canonical answer to "how do I run/install python on this machine".

### Workspace-local Miniconda (one-time install)

```bash
mkdir -p "$DATA_DISK/workspace/envs"
curl -fsSL -o /tmp/miniconda.sh https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash /tmp/miniconda.sh -b -p "$DATA_DISK/workspace/envs/.miniconda"
rm /tmp/miniconda.sh
"$DATA_DISK/workspace/envs/.miniconda/bin/conda" --version
```

No `sudo`. No `conda init`. No `~/.bashrc` edits.

### Create / reuse env (path-based, never `-n`)

```bash
"$DATA_DISK/workspace/envs/.miniconda/bin/conda" create \
  -p "$DATA_DISK/workspace/envs/<env_name>" python=3.11 -y
```

From an `environment.yml`:

```bash
"$DATA_DISK/workspace/envs/.miniconda/bin/conda" env create \
  -p "$DATA_DISK/workspace/envs/<env_name>" \
  -f "$DATA_DISK/workspace/projects/<workspace_name>/environment.yml"
```

### Activate inline before every Python or pip call

```bash
source "$DATA_DISK/workspace/envs/.miniconda/etc/profile.d/conda.sh" && \
conda activate "$DATA_DISK/workspace/envs/<env_name>" && \
python -c 'import sys; print(sys.executable)'
```

`sys.executable` MUST end with `<env_name>/bin/python`. If it does not, the env is wrong — stop, do not run user code.

One-shot alternative (no activation, but explicit env):

```bash
"$DATA_DISK/workspace/envs/<env_name>/bin/python" \
  "$DATA_DISK/workspace/projects/<workspace_name>/<script>"
```

### Install dependencies (only into the profile env)

```bash
source "$DATA_DISK/workspace/envs/.miniconda/etc/profile.d/conda.sh" && \
conda activate "$DATA_DISK/workspace/envs/<env_name>" && \
pip install -r "$DATA_DISK/workspace/projects/<workspace_name>/requirements.txt"
```

PyTorch (CUDA 12.1):

```bash
source "$DATA_DISK/workspace/envs/.miniconda/etc/profile.d/conda.sh" && \
conda activate "$DATA_DISK/workspace/envs/<env_name>" && \
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```

After successful install, lock:

```bash
pip freeze > "$DATA_DISK/workspace/projects/<workspace_name>/requirements.lock.txt"
```

### What NOT to do

```bash
python3 script.py                    # NO — system Python
pip install numpy                    # NO — naked install, lands wherever
conda create -n <name> python=3.11   # NO — name-based env, not path-based
conda activate base                  # NO — base is not for experiments
sudo apt install python3-...         # NO — never sudo for ar-runtime skills
echo 'conda activate ...' >> ~/.bashrc  # NO — never edit shell startup
```

## Cleanup Procedure

Always propose specific paths first. Safe candidates:

```bash
find "$DATA_DISK/workspace/projects" -type d -name __pycache__ -print
find "$DATA_DISK/workspace/projects" -type f -name '*.pyc' -print
du -sh "$DATA_DISK/workspace/artifacts/"* 2>/dev/null | sort -h | tail -20
du -sh "$DATA_DISK/workspace/envs/"*/ 2>/dev/null | sort -h | tail -20
```

Show the list. Wait for confirmation. Then delete with the narrowest possible glob.

## API Keys

API keys live in environment variables only:

```bash
export ANTHROPIC_API_KEY='...'
export OPENAI_API_KEY='...'
```

Never write secrets into `README.md`, YAML, JSON, logs, scripts, or chat output.

## Anti-Patterns

- "Just `pip install <pkg>` to unblock the script" → no, install into the profile env.
- "It worked because base was active" → base is forbidden for experiments.
- `pkill -f python` without first listing matches.
- `rm -rf <path>` with a glob that has not been visually confirmed.
- Editing `~/.bashrc` to make a one-off command convenient.
- Reading `~/.ssh/id_rsa` "just to check it exists".
