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

## Freshness evidence search

Idea Forge's freshness step keeps arXiv as its default free search source. To use
web release pages when checking outdated model, dataset and benchmark mentions,
set this section in `config/providers.local.json`:

```json
{
  "freshness_search": {
    "provider": "parallel",
    "timeout_seconds": 30,
    "proxy_contract": "direct"
  }
}
```

[Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp)
uses Fast mode and works without an account or API key. Install the normal
`requirements.txt`; no extra service setup is needed. The provider sends the
detected stale names and a research objective to `https://search.parallel.ai/mcp`,
then includes returned excerpts and source URLs in the freshness-refresher prompt.
It performs search only; it does not fetch pages or change Claude agent tools.

Use `provider: "none"` to disable network evidence lookup, or `"arxiv"` to keep
the paper search. Python callers can pass `search_provider` to
`step2_5_freshness_refresh` or `refresh_idea_freshness` for a single-call override.
The existing `enable_arxiv=False` argument disables all network evidence lookup,
including an explicit Parallel override. Model routing and credentials are unchanged.

Parallel uses direct connections and the system CA trust store by default. For a configured network
proxy, select the existing `standard-env` contract (`HTTPS_PROXY` / `HTTP_PROXY`,
respecting `NO_PROXY`) or `autoresearch` (`AUTORESEARCH_PROXY_URL`). An unavailable
explicit proxy fails the search; it never falls back to a direct connection.

`timeout_seconds` bounds the entire Parallel exchange for one idea (greater than
zero, at most 300 seconds). Search failure or an anonymous service quota leaves
the refresher using its existing knowledge-library evidence; it never silently
switches search providers. Keyboard interrupts cancel the exchange. A freshness
batch shares one opaque conversation ID across related searches; a new batch or
standalone refresh starts a new ID. Successful refresh records the selected
provider and whether search evidence was used in `freshness_refresh`.

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
