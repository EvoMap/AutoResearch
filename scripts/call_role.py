#!/usr/bin/env python3
"""Small JSON bridge from Bun/MCP consumers to the shared role resolver."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if not separator or not name.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name.strip(), value)


load_env_file(REPO / ".env")

import llm_client  # noqa: E402
import providers  # noqa: E402


def ready_models(role: str) -> tuple[list[str], list[str], list[str], str]:
    config, source, _ = providers.load_effective_config(
        REPO,
        Path(os.environ["AUTORESEARCH_CONFIG"]).expanduser()
        if os.environ.get("AUTORESEARCH_CONFIG") else None,
    )
    configured = llm_client.configured_role_models(role, config)
    profiles = providers.as_profiles(config)
    ready = []
    for name in configured:
        profile = profiles.get(name)
        if not profile or profile.get("api") not in providers.DIALECTS:
            continue
        key = "auth_token" if profile.get("api") == "anthropic_messages" else "api_key"
        required = profile.get("requires_env") or {}
        if (providers.resolve(profile, "base_url")
                and (providers.resolve(profile, key) or providers.resolve(profile, "api_key"))
                and all(os.environ.get(env_name) == wanted
                        for env_name, wanted in required.items())):
            ready.append(name)
    identities = list(dict.fromkeys(
        providers.model_identity(config, model) for model in ready
    ))
    return configured, ready, identities, str(source)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    configured, ready, ready_identities, source = ready_models(args.role)
    if args.self_test:
        print(json.dumps({
            "ok": bool(ready),
            "role": args.role,
            "configured_models": configured,
            "ready_models": ready,
            "ready_model_identities": ready_identities,
            "config": source,
        }, ensure_ascii=False))
        return 0 if ready else 1

    payload = json.load(sys.stdin)
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("stdin JSON must contain a non-empty prompt")
    kwargs = {
        key: payload[key]
        for key in ("temperature", "max_tokens", "system")
        if payload.get(key) is not None
    }
    excluded = payload.get("exclude_identities") or []
    if not isinstance(excluded, list) or not all(isinstance(item, str) for item in excluded):
        raise ValueError("exclude_identities must be an array of strings")
    with redirect_stdout(sys.stderr):
        answer = llm_client.call_role_result(
            args.role, prompt, exclude_identities=excluded, **kwargs)
    if answer is None:
        print(f"all configured models failed for role {args.role}", file=sys.stderr)
        return 1
    print(json.dumps({
        "role": args.role,
        "configured_models": configured,
        "model": answer.model,
        "model_identity": answer.model_identity,
        "text": answer.text,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
