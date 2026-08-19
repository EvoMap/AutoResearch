# LLM Provider Setup

AutoResearch does not require a fixed set of vendors. One JSON file describes every
role, model alias, endpoint, and request dialect; `.env` holds the corresponding
secret values.

## Quick start

```bash
cp .env.example .env
```

The default configuration needs both an OpenAI Chat endpoint for Python roles and an
Anthropic Messages endpoint for the official Claude Code CLI. The two URLs may belong
to the same gateway service, but each must expose its declared wire protocol. Set
`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `ANTHROPIC_BASE_URL`, and `ANTHROPIC_API_KEY` in
`.env`, then load it and verify the routes:

```bash
set -a; . .env; set +a
python scripts/preflight.py --live
```

Exit code 0 means every required role has a model that answered.

## Advanced configuration

Copy the tracked configuration only when you need to change candidates, split roles
across endpoints, or tune request parameters:

```bash
cp config/providers.example.json config/providers.local.json
```

Edit it in this order:

1. Add an `endpoint` with its request `dialect` and the names of its URL/key variables.
2. Add one or more user-defined aliases under `models`, each routed to an endpoint.
3. Put those aliases in the ordered candidate list for each entry under `roles`.

Then put the actual values in `.env`. Never put a real key in the JSON.

```dotenv
MY_GATEWAY_URL=https://gateway.example/v1
MY_GATEWAY_KEY=replace-me
```

The variable names are not prescribed. They only need to match `base_url_env` and
`credential_env` in the JSON. See `docs/unified_provider_config.md` for the complete
schema and resolution contract.

## Supported request dialects

Support is determined by route dialect, not by vendor name:

- `openai_chat`: OpenAI-compatible Chat Completions endpoints.
- `openai_responses`: OpenAI-compatible Responses endpoints.
- `anthropic_messages`: Anthropic Messages-compatible endpoints.

A gateway, official API, self-hosted model, Azure deployment, or other cloud service
works when its route can be represented by a supported dialect. Native Vertex is not
supported; expose it through a supported gateway dialect first.
Model aliases do not imply a dialect: an alias named `strong-model` can point to any
of these routes.

## Role behavior

Most roles try model aliases from left to right until one succeeds. Panel roles such
as `ideator` use all configured aliases. Reviewer and critic MCP tools call the same
Python role resolver through `scripts/call_role.py`; they do not maintain separate
Gemini/GPT credential chains.

The official Claude Code CLI needs a generated local projection:

```bash
set -a; . .env; set +a
python scripts/render_env.py
```

## Verification

```bash
set -a; . .env; set +a
python scripts/preflight.py
python scripts/preflight.py --live
python scripts/verify_provider.py
```

`preflight.py` and runtime use the same effective configuration. `--live` sends real
minimal requests. `verify_provider.py` includes a negative control to ensure an
unknown alias is rejected instead of silently falling back.

## Optional research dependency

GPT Researcher is installed from official upstream at the exact Git revision in
`requirements-research.txt`. Keep it in a separate Python 3.11 environment and call it
through `scripts/research_to_knowledge.py`; its source is not bundled here.

The optional tool reads provider and retriever variables from the environment. It does
not read `providers.local.json`, and changing AutoResearch role routing does not change
its model selection. The adapter requires `--confirm-paid-network` and writes only to a
review directory until a person accepts material into `knowledge_base/`.

## Security

Do not commit `.env`, `config/providers.local.json`, local Claude settings, service
account files, cookies, or logs containing authorization headers. Run
`python scripts/secret_scan.py .` before publishing changes.
