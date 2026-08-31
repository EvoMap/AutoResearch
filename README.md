# AutoResearch

<h2 align="center">From Idea to Paper-Ready Evidence</h2>

<p align="center">
  Insight In, Hallucination Out.
</p>

<p align="center">
  English &nbsp;·&nbsp; <a href="README_CN.md">简体中文</a>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/License-Apache--2.0-47C9E7"></a>
  <a href="https://www.python.org/"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-0B1324"></a>
  <a href="https://evomap.ai"><img alt="EvoMap ecosystem" src="https://img.shields.io/badge/EvoMap-Ecosystem-47C9E7"></a>
  <a href="https://arxiv.org/abs/2608.17906"><img alt="Report: arXiv:2608.17906" src="https://img.shields.io/badge/Report-arXiv%3A2608.17906-B31B1B"></a>
</p>

<p align="center">
  <a href="https://scholar.google.com/citations?user=ayf4nGIAAAAJ">Yiming Ren</a>
  &nbsp;·&nbsp;
  <a href="mailto:liuxiang@evomap.ai">Xiang Liu</a>
  &nbsp;·&nbsp;
  <a href="mailto:sun@evomap.ai">Qumeng Sun</a>
  &nbsp;·&nbsp;
  <a href="mailto:zhangxiao@evomap.ai">Xiao Zhang</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/likaho991007-design">Jiahao Li</a>
</p>

<p align="center">
  Project Leaders:
  <strong><a href="https://autogame-17.github.io/">Haoyang Zhang</a></strong>
  &nbsp;·&nbsp;
  <strong><a href="https://wangjunjie-ai.github.io/">Junjie Wang</a></strong>
</p>

<p align="center">
  Infinite Evolution Lab, <a href="https://evomap.ai">EvoMap</a>
</p>

![AutoResearch workflow from a research idea to reviewable evidence](docs/diagrams/autoresearch-workflow.svg)

AutoResearch is an open-source agent workflow for AI and machine learning research. Give it a research idea, or let it discover directions from recent papers, developer communities, and open-source trends. It continues through experiment planning, implementation, review, execution, result analysis, and independent evaluation to produce an evidence package ready for paper writing.

The workflow is stateful and recoverable. It can iterate based on pilot results and independent review. Research plans, code, run logs, metrics, failure causes, critic reports, and blind reviews are written to disk so researchers can inspect, take over, or stop the process.

![AutoResearch project monitor showing pipeline progress, reviews, and execution rounds](docs/images/autoresearch-project-monitor.png)

## 1. Ways to Use AutoResearch

| Your starting point | Path | Main outputs |
|---|---|---|
| You do not have a specific idea yet | Run Idea Generation | Candidate research directions, reviewed ideas, and experiment plans |
| You already have an idea | Execute the idea directly | Experiment code, run logs, result analysis, and independent review |
| You want the complete workflow | Generate ideas, then select a plan for execution | A complete record from research signals to paper-ready evidence |

## 2. Core Capabilities

| Capability | What you get |
|---|---|
| Cross-domain idea generation | Discover problems from recent external signals, then add constraints and experience from your own domain knowledge |
| Independent multi-model review | Use at least three distinct models during Idea Generation instead of letting one model generate and approve its own work |
| Stateful experiment execution | Persist plans, code, queues, logs, and conclusions so long-running work can resume after interruption |
| Pilot before scaling | Test feasibility at lower cost before starting full experiments or stopping early |
| Traceable evidence and sources | Record Forge sources, knowledge directions, experiment results, critic reports, and blind reviews |
| Support for negative results | Preserve evidence and stop when a hypothesis fails instead of forcing every experiment into a success story |

Research agents can invent missing details when evidence is thin and repeatedly validate their own output. AutoResearch grounds problem discovery in real signals, adds domain knowledge from a local knowledge base, and checks important claims through cross-model review, source records, experiment logs, critic reports, and blind review. These mechanisms reduce unsupported generation, unclear provenance, inflated self-evaluation, and overinterpretation of negative results. The system cannot guarantee that every conclusion is correct, but it preserves the evidence and state needed for researcher review.

## 3. Quickstart

### 3.1 Clone the Repository and Check the Environment

Prepare a Linux machine, either local or accessible over SSH, with Git, Python 3.10+, and `python3-venv` installed:

```bash
git clone https://github.com/EvoMap/AutoResearch.git
cd AutoResearch
bash scripts/bringup.sh
```

`bringup.sh` creates `.venv`, installs Python dependencies, runs the baseline tests and secret scan, and checks the current model configuration. It does not contact model services or incur API charges.

On the first run, before API credentials are configured, a final `BLOCKED` result or nonzero exit is expected. Confirm that Python setup, dependency installation, and tests succeeded, then configure credentials in the next step.

### 3.2 Configure Model Services

Create local configuration files without overwriting existing ones:

```bash
test -f .env || cp .env.example .env
test -f config/providers.local.json || \
  cp config/providers.example.json config/providers.local.json
```

Edit both files:

- `.env` stores real API URLs, keys, proxies, and other machine-local values. Do not commit it.
- `config/providers.local.json` declares endpoints, model aliases, and the models assigned to each role.

AutoResearch does not require a fixed combination of Gemini, GPT, or Claude. You can use one or more compatible endpoints. Stages that require independent opinions count distinct underlying model identities; a single endpoint may expose several different models.

### 3.3 Test the APIs

```bash
set -a
. ./.env
set +a
.venv/bin/python scripts/preflight.py --live
```

This command sends a small number of real requests. Exit code `0` means that normal roles have usable models and that multi-model stages such as Idea Forge and the critic meet their independence requirements.

Then choose a path:

```bash
# Generate ideas
.venv/bin/python idea_generation.py

# Execute an existing idea
# Continue with section 5, "Idea Execution"
```

## 4. Idea Generation: Cross-Domain Discovery

### 4.1 Intersect External Research Signals with Local Domain Knowledge

- **Online research signals:** Collect recent papers, community discussions, and open-source trends, then aggregate, deduplicate, filter, and assess them.
- **Local domain knowledge:** Read research experience, constraints, and common failure patterns maintained by the user in `knowledge_base/`.
- **Cross-domain discovery:** Combine new external signals with local knowledge directions to produce candidate ideas, then run cross-review and experiment planning.

The workflow never rewrites the local knowledge base automatically. You can use the included directions or add your own Markdown files.

Two entrypoints cover full runs and targeted recovery:

- `idea_generation.py` is the recommended entrypoint. It runs online collection, filtering, Idea Forge, and result updates.
- `run_pending_forge.py` resumes seeds already present in `data/pending_forge_seeds.json` without collecting online sources again.

### 4.2 Run the Pipeline

| Stage | What happens |
|---|---|
| 1. Collect | Gather recent research signals from several public channels |
| 2. Filter | Aggregate, deduplicate, screen, and deeply assess candidate directions |
| 3. Intersect | Combine each accepted signal with selected local knowledge directions |
| 4. Generate and review | Ask three or more distinct models to develop ideas independently and cross-review the candidates |
| 5. Plan | Check freshness and agreement, then produce experiment plans for accepted ideas |

Run the default entrypoint:

```bash
.venv/bin/python idea_generation.py
```

Main outputs:

| Path | Contents |
|---|---|
| `data/candidates/` | Aggregated candidate research signals |
| `data/verified/` | Filtering and in-depth assessment results |
| `data/idea_forge/` | Complete ideas, review results, and experiment plans |
| `logs/` | Run logs |

> If a run produces no new seeds, the current implementation searches recent historical verification results for strongly recommended seeds and records that fallback in the logs.

### 4.3 Select Local Knowledge Directions

List the available directions or filter them by keyword:

```bash
.venv/bin/python src/idea_forge/b_library.py
.venv/bin/python src/idea_forge/b_library.py agent
```

The four registered directions are used by default. To choose your own combination, add the following to `config/providers.local.json`:

```json
{
  "idea_forge": {
    "b_directions": ["Agent_运行时与沙箱", "视觉推理"]
  }
}
```

Each direction name corresponds to a Markdown filename under `knowledge_base/`. Adding directions increases generation and review calls; start with a small set when validating a new setup.

### 4.4 Optional: Draft a Knowledge Direction with GPT Researcher

The repository does not bundle GPT Researcher source code, and Idea Generation does not invoke it automatically. To gather material for a new direction, install the pinned upstream release in a separate Python 3.11 environment:

```bash
python3.11 -m venv .venv-research
.venv-research/bin/python -m pip install -r requirements-research.txt
```

The upstream tool does not read `config/providers.local.json`. It reads environment variables directly. The default setup expects `OPENAI_API_KEY` and `TAVILY_API_KEY` in `.env`; use upstream variables such as `FAST_LLM`, `SMART_LLM`, and `RETRIEVER` to change models or retrieval backends.

The following command accesses the network and may incur model and retrieval charges, so it requires explicit confirmation:

```bash
set -a
. ./.env
set +a
.venv-research/bin/python scripts/research_to_knowledge.py \
  "agent runtime safety" \
  --confirm-paid-network
```

Drafts are written only to the Git-ignored `workspaces/knowledge-drafts/` directory. Review sources, remove incorrect content, and complete the sections required by `knowledge_base/TEMPLATE.md` before moving accepted material into `knowledge_base/`. The adapter never rewrites the formal knowledge base automatically.

### 4.5 Select and Export an Idea

Idea Generation does not choose the final plan for execution. First list executable plans:

```bash
.venv/bin/python src/idea_provenance.py list \
  --forge-file data/idea_forge/forge_YYYYMMDD_HHMM.json
```

Then export a selected plan to `data/ideas/`:

```bash
.venv/bin/python src/idea_provenance.py export \
  --forge-file data/idea_forge/forge_YYYYMMDD_HHMM.json \
  --result-index 1 \
  --plan-index 1 \
  --output data/ideas/my_experiment.txt
```

Both indices start at `1`. The exported file records the Forge file checksum, seed index, plan index, and knowledge directions for downstream experiment and dashboard provenance.

<details>
<summary><strong>90-day mode, checkpoint recovery, and pending seeds</strong></summary>

Collect signals from a roughly 90-day window:

```bash
touch trigger_3month.txt
.venv/bin/python idea_generation.py
```

The trigger file is deleted automatically after the run starts.

Forge saves every completed seed atomically. Set a fixed checkpoint path and restart with the same path to skip completed seeds:

```bash
export AR_FORGE_CHECKPOINT=data/idea_forge/my_forge_checkpoint.json
.venv/bin/python idea_generation.py
```

You may change `execution.max_concurrency` in `config/providers.local.json` while a run is active. The next batch of independent tasks reads the new value; requests already in flight are not interrupted.

To resume Forge from seeds already written to `data/pending_forge_seeds.json` without collecting online sources again:

```bash
.venv/bin/python run_pending_forge.py
```

</details>

## 5. Idea Execution

If you already have an idea, start here. The execution workflow lives in `ar-runtime/` and uses the official Claude Code CLI to advance one idea through a recoverable experiment project.

### 5.1 Prepare the Execution Environment

In addition to the Python environment, install or provide:

- Bun 1.3+
- Node.js, which is used by the `bun install` lifecycle scripts
- Conda or another suitable Python environment manager for experiments
- The CPU, GPU, data, and disk resources required by the experiment
- The Ralph Loop plugin for automatic workflow continuation

Install runtime dependencies:

```bash
cd ar-runtime
bun install --frozen-lockfile
cd ..
```

If Bun is installed in `~/.bun/bin` but is not on `PATH`, add this line to your shell configuration and reconnect:

```bash
export PATH="$HOME/.bun/bin:$PATH"
```

### 5.2 Generate the Execution Configuration

Create machine-local settings from the safe template, then project the unified provider configuration into the execution loop:

```bash
test -f ar-runtime/.claude/settings.local.json || \
  cp ar-runtime/.claude/settings.local.example.json \
     ar-runtime/.claude/settings.local.json

set -a
. ./.env
set +a
.venv/bin/python scripts/render_env.py
.venv/bin/python scripts/preflight.py --live --tools
```

`ar-runtime/.claude/settings.local.json` is ignored by Git. Projection configures the Claude Code execution loop; the reviewer and critic MCP servers read their role routes directly from the same unified JSON configuration.

The final command also checks two rounds of tool calls. It can detect endpoints that accept a single model request but fail on multi-agent tool messages.

### 5.3 Prepare an Idea

Ideas can come from either source:

- `data/ideas/*.txt` files exported by Idea Generation.
- Text or Markdown files you write yourself.

At minimum, describe the research hypothesis, available data, success metrics, and compute and time constraints. The simplest path is:

```text
data/ideas/my_experiment.txt
```

`b_id` must resolve through `src/idea_forge/b_library.py`. Initialization and dashboard generation fail clearly if the name is misspelled or the knowledge file does not exist. Forge exports include this metadata automatically; do not rewrite it manually.

### 5.4 Start the Coordinator

The current Alpha entrypoint grants Claude Code broad tool permissions. Run it only in an isolated, disposable task environment. Do not mount the host home directory, SSH agent, cloud credentials, customer data, or unrelated project directories.

Install and start the official Claude Code CLI:

```bash
cd ar-runtime
claude --dangerously-skip-permissions
```

If Ralph Loop is unavailable after startup, install and enable `ralph-loop@claude-plugins-official` through the Claude Code `/plugin` interface.

Inside Claude Code, run:

```text
/ar-coordinator ../data/ideas/my_experiment.txt ../data/projects/my_experiment
```

For non-interactive runs, use the supervisor. It reaps the process group, retries terminal API failures within a restart budget, and stores a manifest for every attempt:

```bash
cd ar-runtime
scripts/ar-supervisor.sh \
  ../data/ideas/my_experiment.txt \
  ../data/projects/my_experiment
```

### 5.5 How an Experiment Proceeds

| Stage | Main actions |
|---|---|
| Initialize and plan | Freeze idea provenance, create project state, and generate and review the experiment plan |
| Pilot | Implement and review code, then validate feasibility at smaller scale |
| Scale or stop | Use pilot results to start the main experiment, revise the plan, or stop with a preserved negative result |
| Main experiment and analysis | Run full experiments and organize metrics, logs, failure causes, and findings |
| Independent review | Ask the critic to challenge conclusions, then run blind review without self-evaluation context |
| Close or iterate | Close when completion conditions pass; otherwise append the next concrete work unit |

Each step advances one persistable work unit. After an interruption, use the same idea and project directory to continue:

```text
/ar-coordinator ../data/ideas/my_experiment.txt ../data/projects/my_experiment continue the workflow
```

The coordinator emits the following marker only after the queue is complete and closure checks pass:

```xml
<promise>AUTORESEARCH_DONE</promise>
```

### 5.6 Inspect Project Results

Each project is stored under `data/projects/<project_name>/`:

| File or directory | Contents |
|---|---|
| `idea.md`, `idea_provenance.json` | Frozen idea text and provenance |
| `plan.md` | Experiment plan, metrics, and success criteria |
| `workflow_queue.json`, `state.md` | Recoverable work queue and current state |
| `decisions.log` | Append-only decision log |
| `code/` | Experiment code |
| `review.md` | Plan and code review |
| `results/` | Run logs, metrics, and result summaries |
| Critic and blind-review files | Final independent evaluations |

Generate a dashboard for one project:

```bash
.venv/bin/python src/generate_project_dashboard.py my_experiment
```

Generate the project overview:

```bash
.venv/bin/python src/generate_project_dashboard.py --all
```

Generate the knowledge base board:

```bash
.venv/bin/python src/generate_kb_dashboard.py
```

![AutoResearch knowledge base board showing public knowledge directions and document summaries](docs/images/autoresearch-knowledge-base-board.png)

<details>
<summary><strong>Run a GPU smoke test first</strong></summary>

After a GPU is available, use the included matrix-multiplication idea to confirm that the execution path actually uses it:

```bash
cd ar-runtime
claude --dangerously-skip-permissions \
  -p "/ar-coordinator ../examples/idea_gpu_smoke.txt ../data/projects/gpu_smoke"
```

This separates environment failures from problems in a research idea. It does not represent the compute requirements of a real experiment.

</details>

## 6. Model and Role Configuration

AutoResearch has one model configuration entrypoint: `config/providers.local.json`. The Python pipeline, preflight checks, reviewer MCP, and critic MCP all read this file. Real keys belong only in `.env`.

Roles fall into three groups:

<table>
  <thead>
    <tr>
      <th>Stage</th>
      <th>Role</th>
      <th>Purpose</th>
      <th>Model requirement</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th rowspan="3" scope="rowgroup"><code>░ Idea signal filtering</code></th>
      <td><code>screener</code></td>
      <td>Quickly screen online research signals</td>
      <td>One model</td>
    </tr>
    <tr>
      <td><code>judge</code></td>
      <td>Assess the research value of candidate signals in depth</td>
      <td>One model</td>
    </tr>
    <tr>
      <td><code>consensus_checker</code></td>
      <td>Check whether repeated assessments genuinely agree</td>
      <td>One model</td>
    </tr>
    <tr>
      <th rowspan="3" scope="rowgroup"><code>▒ Idea generation and validation</code></th>
      <td><code>ideator</code></td>
      <td>Develop ideas independently through cross-domain discovery and cross-review them</td>
      <td>At least 3 distinct models</td>
    </tr>
    <tr>
      <td><code>planner</code></td>
      <td>Turn an accepted idea into an experiment plan</td>
      <td>One model</td>
    </tr>
    <tr>
      <td><code>freshness_refresher</code></td>
      <td>Refresh the plan with newer models, datasets, and baselines</td>
      <td>One model</td>
    </tr>
    <tr>
      <th rowspan="4" scope="rowgroup"><code>▓ Idea execution</code></th>
      <td><code>agent</code></td>
      <td>Drive coordination, planning, implementation, and experiments</td>
      <td>One model</td>
    </tr>
    <tr>
      <td><code>code_reviewer</code></td>
      <td>Review experiment plans and code</td>
      <td>One model</td>
    </tr>
    <tr>
      <td><code>critic</code> + optional <code>critic_secondary</code></td>
      <td>Challenge completion and provide memory-independent blind review</td>
      <td>1 model for the primary role; 2 distinct models when the secondary role is enabled</td>
    </tr>
    <tr>
      <td><code>run_monitor</code></td>
      <td>Compress long-running logs into progress summaries</td>
      <td>Optional; one model when enabled</td>
    </tr>
  </tbody>
</table>

You define model aliases. The following fragment shows role mapping only; each name must also be declared under `models` in the same JSON file:

```json
{
  "request_defaults": {"max_tokens": 8192},
  "roles": {
    "screener": {"models": ["gemini-3.1-flash-lite"]},
    "judge": {"models": ["gpt-5.5"]},
    "consensus_checker": {"models": ["gpt-5.5"]},
    "ideator": {
      "models": ["claude-opus-4.8", "gemini-3.1-pro", "gpt-5.5"]
    },
    "planner": {"models": ["claude-opus-4.8"]},
    "freshness_refresher": {"models": ["gpt-5.5"]},
    "agent": {"models": ["claude-opus-4.8"]},
    "code_reviewer": {"models": ["gemini-3.1-pro"]},
    "critic": {"models": ["gpt-5.5"]},
    "critic_secondary": {"_optional": true, "models": ["gemini-3.1-pro"]},
    "run_monitor": {"models": ["gemini-3.1-flash-lite"]}
  }
}
```

Configuration rules:

- Normal roles use one model per call and try candidates from left to right.
- `ideator` calls every configured seat and requires at least three distinct models.
- `critic` must be available. `critic_secondary` is skipped explicitly when no usable route or credential exists.
- When enabled, `critic_secondary` must pass a real request and resolve to a model distinct from `critic`.
- Aliases or endpoints that resolve to the same underlying model still count as one model; distinct models may share an endpoint.
- Each endpoint makes up to three total attempts for temporary network errors, `429`, and `5xx` responses.
- `request_defaults.max_tokens` sets the default output limit for business-model calls.
- `execution.max_concurrency` controls concurrent Idea Forge requests and defaults to `3`.
- `AR_LLM_TIMEOUT` controls the timeout for one model request and defaults to `900` seconds.

See [Unified Provider Configuration](docs/unified_provider_config.md) for complete endpoint, model, and route examples.

## 7. FAQ

### 7.1 Must I Use Gemini, GPT, and Claude Together?

No. Normal roles may share one model. Idea Forge requires three distinct models, and two enabled critic roles must also use distinct models. They may come from one provider or one compatible endpoint.

### 7.2 I Already Have an Idea. Do I Need Online Collection?

No. Write the idea under `data/ideas/` and start the coordinator from section 5.

### 7.3 Can I Use AutoResearch without a GPU?

Idea Generation runs on a CPU machine. GPU requirements for Idea Execution depend on the experiment. The workflow starts with a pilot so resource mismatches can be found early.

### 7.4 What If an Online Channel Returns 403?

External sites may restrict regions, request rates, or egress IPs. Networks in some regions, including mainland China, may require a proxy. When one collection channel fails, the pipeline records the failure, skips that channel, and continues.

### 7.5 Why Can Idea Generation Take a Long Time?

Call volume grows with the number of seeds, knowledge directions, and ideator seats, and cross-review calls every seat again. Validate the workflow with a small set of knowledge directions and the default concurrency before scaling up.

## 8. Project Structure

```text
idea_generation.py              Cross-domain Idea Generation entrypoint
src/                            Collection, filtering, model routing, and Idea Forge
config/providers.example.json   Unified role and provider configuration template
knowledge_base/                 Local domain knowledge base
data/ideas/                     Ideas ready for execution
data/projects/                  Experiment projects, state, and results
ar-runtime/                     Stateful multi-agent execution runtime
scripts/                        Environment, validation, and configuration tools
```

Further documentation:

- [Unified Provider Configuration](docs/unified_provider_config.md)
- [Model Provider Setup and Validation](docs/llm_provider_setup.md)
- [Execution State Machine](ar-runtime/ar-coordinator-startup-flow.md)

## 9. Citation

If you use AutoResearch in a paper or project, cite the software:

```bibtex
@software{ren2026autoresearch,
  author = {
    Yiming Ren and
    Xiang Liu and
    Qumeng Sun and
    Xiao Zhang and
    Jiahao Li and
    Haoyang Zhang and
    Junjie Wang
  },
  title = {AutoResearch},
  year = {2026},
  url = {https://github.com/EvoMap/AutoResearch}
}
```
