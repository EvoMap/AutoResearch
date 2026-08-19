# Unified provider configuration

AutoResearch has one authoritative JSON configuration:

```text
config/providers.local.json     local, ignored
config/providers.example.json   tracked template and defaults
```

The local file overlays the tracked template. Every consumer—preflight, the Python
idea pipeline, Claude Code projection, MCP reviewers, and experiment agents—must
resolve roles through this effective configuration.

## What belongs in JSON

The JSON describes names and relationships, never credential values:

```json
{
  "version": 2,
  "endpoints": {
    "main-gateway": {
      "dialect": "openai_chat",
      "base_url_env": "MAIN_API_BASE_URL",
      "credential_env": ["MAIN_API_KEY"]
    }
  },
  "models": {
    "fast-model": {
      "capabilities": {},
      "routes": [
        {"endpoint": "main-gateway", "wire_name": "model-fast"}
      ]
    },
    "strong-model": {
      "capabilities": {"tools": true},
      "routes": [
        {"endpoint": "main-gateway", "wire_name": "model-strong"}
      ]
    }
  },
  "roles": {
    "screener": {"models": ["fast-model"]},
    "judge": {"models": ["strong-model"]},
    "ideator": {"models": ["strong-model", "fast-model"]},
    "planner": {"models": ["strong-model"]},
    "agent": {"models": ["strong-model"]},
    "code_reviewer": {"models": ["strong-model"]},
    "critic": {"models": ["strong-model"]}
  }
}
```

Names such as `main-gateway` and `strong-model` are user-defined aliases. Provider
brands and model families are examples, not part of the runtime contract.

## What belongs in `.env`

The variables named by JSON hold the private values:

```bash
MAIN_API_BASE_URL=https://gateway.example/v1
MAIN_API_KEY=...
```

`.env` is ignored. JSON may contain a public literal `base_url` or the name of an
environment variable that supplies it. API keys, tokens, service-account bodies, and
other secret values must be referenced by environment-variable name and kept out of
JSON.

## Resolution contract

1. A role selects the ordered model aliases in `roles.<role>.models`.
2. A model alias selects its ordered routes.
3. A route selects an endpoint, wire model name, and request parameters.
4. An endpoint selects the dialect, transport, URL variable, and credential variables.
5. `AR_MODEL_<ROLE>` may replace a role's model aliases for one process.
6. Explicit user aliases win over built-in aliases with the same name.
7. A consumer must not maintain another provider/model fallback list in source code.

Panel roles such as `ideator` may use several model aliases. Independence is based on
resolved model identity, not provider branding. Single-seat roles try their configured
models in order.

## Verification contract

The following commands must report the same effective config path and digest:

```bash
bash scripts/bringup.sh
python scripts/preflight.py
python scripts/render_env.py
python scripts/verify_provider.py
```

Tests use a provider-neutral fixture whose endpoint and model aliases contain none of
`gemini`, `gpt`, or `claude`. The fixture must reach the Python pipeline, Idea Forge,
MCP reviewer/critic, and experiment-agent launch path without changing source files.
