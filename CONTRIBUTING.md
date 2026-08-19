# Contributing to AutoResearch

Thanks for your interest in AutoResearch. This repository contains research-system code and evidence-oriented documentation, so contributions should preserve reproducibility and claim discipline.

## Ground rules

- Never commit API keys, auth tokens, cookies, passwords, service-account JSON files, `.env` files, local Claude settings, logs with headers, or generated private data.
- Use `config/providers.example.json` and `.env.example` as templates. Keep real values in ignored local files or environment variables.
- Keep claims tied to evidence. If a result is protocol-level, tool-level, or workflow-level, label it accordingly.
- Prefer small, reviewable pull requests.

## Development setup

```bash
bash scripts/bringup.sh                 # venv + deps + tests + credential check
pip install pre-commit
pre-commit install
pre-commit install --hook-type pre-push
cp config/providers.example.json config/providers.local.json
cp .env.example .env
```

The two `pre-commit install` lines are not optional and cannot be enforced by a
commit: git will not let a repository set `core.hooksPath` on your behalf. Without
them the local gates simply do not exist, and you find out on CI instead. Commit
hooks run the fast lint, model-reference, tracked-projection, and public-knowledge
checks. The secret scan runs on push.

The hooks resolve an interpreter through `scripts/hook_python.sh`: this tree's
`.venv` first, then the main worktree's, then any python that can import the
dependencies. So a linked worktree borrows the main tree's venv rather than
needing its own, and when none of them work the hook says which command fixes it
instead of failing with a bare `Executable not found`.

`config/providers.local.json` is the single runtime configuration. It maps roles to
user-defined model aliases and routes; it contains environment-variable names, not
secret values. Put those values in `.env` and load it before manual commands.

Check what is reachable before spending anything:

```bash
python scripts/preflight.py
```

It reports, per role, which variable is missing, and sends no request. `--live`
sends one minimal real request per profile.

There are two `.env.example` files. The one at the repository root is this
pipeline's own template: it lists variables named by `config/providers.example.json`.
The root template covers the variables the tracked provider config names. A
`config/providers.local.json` of your own may name others, so read what is missing from
`python scripts/preflight.py` rather than from the template.

## Tests

Before sending a PR, run the offline checks. None of them needs a paid credential:

```bash
ruff check .
python -m pytest tests/ ar-runtime/scripts/tests/ -q
python scripts/check_model_references.py
python scripts/render_env.py --check
python scripts/secret_scan.py .
python scripts/check_release_tree.py .
python scripts/check_public_knowledge.py .
```

If you touched `ar-runtime/scripts/*.ts`, run that directory's own checks as well. They need bun:

```bash
cd ar-runtime
bun install --frozen-lockfile
bun run typecheck
bunx biome check scripts/ar-*.ts
bun test scripts/ar-*.test.ts
```

CI runs a superset of the above. Two of its checks are deliberately not asked of you here: it fails the run
when pytest reports any test as `skipped`, which locally you can just read off the same output; and it asserts
the exit-code contract of `ar-runtime/scripts/ar-preflight-mcp.sh` in both directions, which needs a scrubbed
environment. Repository file checks run against the checkout locally and on CI with the same criteria.
`tests/test_ci_gate_still_runs_the_checks.py` reconciles this section against
the workflow, so a check added to CI without a line here turns that test red.

The pre-commit hooks are a fast subset, not a replacement. Live LLM integration tests should be marked
explicitly and should not run by default in CI.

## Publishing

Releases are cut by maintainers from reviewed commits. Contributors only need to run the checks above and describe their evidence in the pull request.

## Security-sensitive changes

Changes touching provider routing, credential loading, Twitter/X collection, remote execution, Docker, or release packaging require extra review.

