# Secrets and Credentials Policy

AutoResearch depends on several paid APIs and optional authenticated data sources. The repository must never contain real credentials.

## Never commit

- `.env`, `.env.*` except sanitized `.env.example`
- `config.json`, `config.local.json`, or other private config files
- `.claude/` and `settings.local.json`
- API keys, bearer tokens, auth tokens, cookies, passwords, SSH keys, or service-account JSON files
- Twitter/X cookies, passwords, `auth_token` values, or session databases
- logs that may contain request headers, environment variables, prompts, or private paths

## Safe template pattern

Use placeholders only:

```bash
OPENAI_API_KEY=your_openai_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
TAVILY_API_KEY=your_tavily_api_key_here
```

Do not use real-looking fake keys. If a template value starts with `sk-`, `AIza`, or `tvly-`, scanners and users may treat it as a leaked secret.

## Rotation rule

If a secret has ever been committed, uploaded, shared in an archive, pasted into a prompt, or included in a screenshot, rotate it. Removing it from a file is not enough; git history, caches, logs, and backups may still contain it.

## Local setup

Use:

- `.env` for local shell variables.
- `config/providers.local.json` for private provider routing.
- `GOOGLE_APPLICATION_CREDENTIALS` pointing to a service-account file outside the repo.
- a secrets manager for production deployments.

## Before sharing changes

Run `python scripts/secret_scan.py .` and manually inspect every reported file. Do not copy scanner output containing secrets into GitHub issues or chat logs.
