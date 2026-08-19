#!/usr/bin/env python3
"""Deterministic role bridge used by MCP process and CI contract tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


role = sys.argv[sys.argv.index("--role") + 1]
secondary_configured = os.environ.get("FAKE_SECONDARY_CONFIGURED") == "1"

if "--self-test" in sys.argv:
    ready = role != "critic_secondary" or secondary_configured
    print(json.dumps({
        "ok": ready,
        "role": role,
        "configured_models": [f"{role}-model"] if ready else [],
        "ready_models": [f"{role}-model"] if ready else [],
        "ready_model_identities": ["model-b" if role == "critic_secondary" else "model-a"]
        if ready else [],
        "config": "fake.json",
    }))
    raise SystemExit(0 if ready else 1)

call_log = os.environ.get("FAKE_ROLE_CALL_LOG")
if call_log:
    with Path(call_log).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": role}) + "\n")
if role == "critic_secondary" and os.environ.get("FAKE_SECONDARY_FAIL") == "1":
    print("injected secondary live failure", file=sys.stderr)
    raise SystemExit(9)

request = json.load(sys.stdin)
request_log = os.environ.get("FAKE_ROLE_REQUEST_LOG")
if request_log:
    with Path(request_log).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "role": role,
            "max_tokens": request.get("max_tokens"),
        }) + "\n")
prompt = str(request.get("prompt") or "")
prompt_log = os.environ.get("FAKE_ROLE_PROMPT_LOG")
if prompt_log:
    Path(prompt_log).write_text(prompt, encoding="utf-8")
if "external AutoResearch critic" in prompt:
    text = (
        "- verdict: finish_ok\n"
        "- confidence: high\n"
        "- required_next_focus: none\n"
        "- optional_next_focus: none\n"
        "- stop_reason: deterministic fixture found no blocking gap\n"
    )
elif "senior research engineer reviewing experiment code" in prompt:
    blockers_count = os.environ.get("FAKE_REVIEW_BLOCKERS_COUNT", "0")
    text = (
        "```markdown\n"
        "---\n"
        f"blockers_count: {blockers_count}\n"
        "warnings_count: 0\n"
        "files_reviewed: 1\n"
        "reviewer_role: code_reviewer\n"
        "---\n\n"
        "# Review\n\n"
        "## Blockers (must fix before running)\n"
        "None.\n\n"
        "## Warnings (should fix)\n"
        "None.\n\n"
        "## Constraint Audit\n"
        "- [C1] Code bundle is reviewable | status: satisfied | "
        "evidence: supplied code bundle | blocker: none\n\n"
        "## Notes\n"
        "- Deterministic fixture review.\n\n"
        "## Overall\n"
        "Approved for the contract test.\n"
        "```"
    )
else:
    text = "SELF_TEST_OK"

print(json.dumps({
    "role": role,
    "configured_models": [f"{role}-model"],
    "model": f"{role}-model",
    "model_identity": "model-b" if role == "critic_secondary" else "model-a",
    "text": text,
}))
