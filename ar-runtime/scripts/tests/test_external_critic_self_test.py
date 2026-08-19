from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[3]
MCP = REPO / "ar-runtime" / "scripts" / "ar-external-critic-mcp.ts"
BRIDGE = REPO / "ar-runtime" / "scripts" / "tests" / "fixtures" / "fake_role_bridge.py"
FIND_BUN = REPO / "scripts" / "find-bun.sh"


def bun_binary() -> str:
    found = subprocess.run([str(FIND_BUN)], capture_output=True, text=True, timeout=10)
    if found.returncode == 1:
        pytest.skip("bun is not installed; external critic MCP self-test cannot run")
    assert found.returncode in {0, 3}, found.stderr
    return found.stdout.strip()


def run_self_test(calls: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "AUTORESEARCH_PYTHON": str(BRIDGE),
        "FAKE_ROLE_CALL_LOG": str(calls),
        **env_overrides,
    })
    return subprocess.run(
        [bun_binary(), str(MCP), "--self-test"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def called_roles(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return [json.loads(line)["role"] for line in calls.read_text(encoding="utf-8").splitlines()]


def test_self_test_live_calls_both_configured_critics(tmp_path: Path) -> None:
    calls = tmp_path / "calls.jsonl"

    done = run_self_test(calls, FAKE_SECONDARY_CONFIGURED="1")

    assert done.returncode == 0, done.stderr
    assert called_roles(calls) == ["critic", "critic_secondary"]
    assert '"independent_pair":true' in done.stderr


def test_self_test_rejects_an_unconfigured_secondary(tmp_path: Path) -> None:
    calls = tmp_path / "calls.jsonl"

    done = run_self_test(calls)

    assert done.returncode == 1, done.stderr
    assert called_roles(calls) == ["critic"]
    assert '"status":"skipped"' in done.stderr


def test_self_test_fails_when_a_configured_secondary_live_call_fails(tmp_path: Path) -> None:
    calls = tmp_path / "calls.jsonl"

    done = run_self_test(
        calls,
        FAKE_SECONDARY_CONFIGURED="1",
        FAKE_SECONDARY_FAIL="1",
    )

    assert done.returncode == 1, done.stderr
    assert called_roles(calls) == ["critic", "critic_secondary"]
    assert "injected secondary live failure" in done.stderr
