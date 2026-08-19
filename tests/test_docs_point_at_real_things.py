"""Public delivery documents must describe files and roles that actually exist."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import providers  # noqa: E402


DOCS = (
    "README.md",
    "README_CN.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "ar-runtime/README.md",
    "ar-runtime/ar-coordinator-startup-flow.md",
    "docs/llm_provider_setup.md",
    "docs/secrets.md",
    "docs/unified_provider_config.md",
)

RUNTIME_GENERATED = {
    ".claude/settings.local.json",
    "ar-runtime/.claude/settings.local.json",
    "config.json",
    "data/pending_forge_seeds.json",
    "providers.local.json",
    ".env",
    "config.local.json",
    "config/providers.local.json",
    "decisions.log",
    "idea.md",
    "idea_provenance.json",
    "plan.md",
    "results/run.log",
    "results/summary.md",
    "review.md",
    "settings.local.json",
    "state.md",
    "trigger_3month.txt",
    "workflow_queue.json",
    "workflow_queue.engine.json",
}


def tracked() -> set[str]:
    return set(
        subprocess.run(
            ["git", "-C", str(REPO), "ls-files"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    )


def referenced_paths(document: str) -> set[str]:
    text = (REPO / document).read_text(encoding="utf-8")
    references = set(
        re.findall(r"`([A-Za-z0-9_./*-]+\.(?:py|sh|ts|json|md|css|yml|txt))`", text)
    )
    references |= set(
        re.findall(r"(?:python3?|bash)\s+([A-Za-z0-9_./-]+\.(?:py|sh))", text)
    )
    return references


def test_every_file_named_by_delivery_docs_exists_or_is_runtime_generated() -> None:
    known = tracked()
    by_name = {Path(path).name for path in known}
    missing: dict[str, list[str]] = {}

    for document in DOCS:
        absent = []
        for reference in sorted(referenced_paths(document)):
            if "*" in reference or reference in known or reference in RUNTIME_GENERATED:
                continue
            if Path(reference).name in by_name:
                continue
            if any(path.endswith("/" + reference) for path in known):
                continue
            absent.append(reference)
        if absent:
            missing[document] = absent

    assert not missing, f"delivery documents contain missing paths: {missing}"


def test_runtime_readme_idea_paths_resolve_to_tracked_examples() -> None:
    doc = REPO / "ar-runtime" / "README.md"
    references = set(
        re.findall(
            r"\.\./[A-Za-z0-9_./-]+\.(?:md|txt)",
            doc.read_text(encoding="utf-8"),
        )
    )
    known = tracked()
    missing = [
        reference
        for reference in sorted(references)
        if not reference.startswith("../examples/ideas/")
    ]

    assert references, "ar-runtime README has no verifiable Idea example path"
    for reference in sorted(references):
        resolved = (doc.parent / reference).resolve()
        if not resolved.is_relative_to(REPO):
            missing.append(reference)
            continue
        if resolved.relative_to(REPO).as_posix() not in known:
            missing.append(reference)

    assert not missing, f"ar-runtime README has missing or untracked Idea paths: {missing}"


def test_external_research_docs_use_the_official_adapter_contract() -> None:
    documents = (
        "README.md",
        "docs/llm_provider_setup.md",
        "knowledge_base/README.md",
    )
    required = ("requirements-research.txt", "scripts/research_to_knowledge.py")
    missing = {
        document: [item for item in required if item not in (REPO / document).read_text(encoding="utf-8")]
        for document in documents
    }

    assert not any(missing.values()), f"research adapter contract is incomplete: {missing}"


def test_root_env_template_covers_the_tracked_provider_config() -> None:
    config = json.loads((REPO / "config/providers.example.json").read_text(encoding="utf-8"))
    named = providers.referenced_env_names(config)
    template = {
        line.split("=", 1)[0].strip()
        for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    assert named
    assert named <= template, f"provider variables missing from .env.example: {sorted(named - template)}"


def test_every_documented_model_override_is_a_config_role() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    config = json.loads((REPO / "config/providers.example.json").read_text(encoding="utf-8"))
    documented = {match.lower() for match in re.findall(r"AR_MODEL_([A-Z_]+)=", readme)}

    assert documented <= set(config["roles"])


def test_every_runtime_role_is_declared_in_the_config() -> None:
    import llm_client

    config = json.loads((REPO / "config/providers.example.json").read_text(encoding="utf-8"))

    assert set(llm_client.ROLE_DEFAULTS) <= set(config["roles"])


def test_every_config_role_resolves_to_a_declared_model() -> None:
    config = json.loads((REPO / "config/providers.example.json").read_text(encoding="utf-8"))
    known = set(providers.as_profiles(config))
    dangling = {
        role: [model for model in providers.role_candidates(config, role) if model not in known]
        for role in config["roles"]
    }

    assert not any(dangling.values()), f"roles reference missing models: {dangling}"
