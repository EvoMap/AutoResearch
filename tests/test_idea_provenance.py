from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import idea_provenance


def idea_with_b_id(b_id: str, body: str = "# Experiment\n\nRun the pilot.\n") -> str:
    return (
        "<!-- autoresearch-provenance\n"
        f'{{"schema_version": 1, "b_id": "{b_id}"}}\n'
        "-->\n\n"
        f"{body}"
    )


def test_plain_idea_files_remain_valid(tmp_path: Path) -> None:
    source = tmp_path / "plain.txt"
    source.write_text("# Hand-written idea\n\nRun it.\n", encoding="utf-8")

    artifact = idea_provenance.load_idea(source)

    assert artifact.b_id is None
    assert artifact.body == "# Hand-written idea\n\nRun it.\n"


def test_structured_provenance_is_removed_from_the_executable_idea(tmp_path: Path) -> None:
    source = tmp_path / "forged.txt"
    source.write_text(idea_with_b_id("mllm_visual_tokens"), encoding="utf-8")

    artifact = idea_provenance.load_idea(source)

    assert artifact.b_id == "mllm_visual_tokens"
    assert artifact.body == "# Experiment\n\nRun the pilot.\n"
    assert "autoresearch-provenance" not in artifact.body


@pytest.mark.parametrize(
    "text, error",
    [
        (idea_with_b_id("definitely_missing"), "unknown B direction"),
        (
            "<!-- autoresearch-provenance\nnot-json\n-->\n\nBody\n",
            "invalid JSON",
        ),
        (
            '<!-- autoresearch-provenance\n{"schema_version": 2, "b_id": "mllm_visual_tokens"}\n-->\n\nBody\n',
            "schema_version",
        ),
    ],
)
def test_declared_provenance_fails_closed(tmp_path: Path, text: str, error: str) -> None:
    source = tmp_path / "bad.txt"
    source.write_text(text, encoding="utf-8")

    with pytest.raises(idea_provenance.IdeaProvenanceError, match=error):
        idea_provenance.load_idea(source)


def test_prepare_materializes_an_immutable_project_input(tmp_path: Path) -> None:
    source = tmp_path / "forged.txt"
    source.write_text(idea_with_b_id("mllm_visual_tokens"), encoding="utf-8")
    project = tmp_path / "project"

    first = idea_provenance.prepare_project_idea(source, project)
    second = idea_provenance.prepare_project_idea(source, project)

    assert first == second
    assert project.joinpath("idea.md").read_text(encoding="utf-8") == "# Experiment\n\nRun the pilot.\n"
    stored = json.loads(project.joinpath("idea_provenance.json").read_text(encoding="utf-8"))
    assert stored["b_id"] == "mllm_visual_tokens"
    assert stored["idea_sha256"] == first["idea_sha256"]
    assert stored["source_sha256"] == first["source_sha256"]
    assert stored["binding_sha256"] == idea_provenance.manifest_binding_sha256(stored)
    assert str(tmp_path) not in stored["source_file"]

    source.write_text(idea_with_b_id("mllm_visual_tokens", "Changed.\n"), encoding="utf-8")
    with pytest.raises(idea_provenance.IdeaProvenanceError, match="already bound"):
        idea_provenance.prepare_project_idea(source, project)


def test_prepare_refuses_to_overwrite_an_unmanaged_project_idea(tmp_path: Path) -> None:
    source = tmp_path / "plain.txt"
    source.write_text("Source\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    project.joinpath("idea.md").write_text("User-owned content\n", encoding="utf-8")

    with pytest.raises(idea_provenance.IdeaProvenanceError, match="refusing to overwrite"):
        idea_provenance.prepare_project_idea(source, project)


def test_project_binding_detects_a_changed_b_id(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text(idea_with_b_id("mllm_visual_tokens"), encoding="utf-8")
    project = tmp_path / "project"
    idea_provenance.prepare_project_idea(source, project)
    path = project / "idea_provenance.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["b_id"] = "llm_reasoning"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(idea_provenance.IdeaProvenanceError, match="binding_sha256"):
        idea_provenance.load_project_provenance(project)


@pytest.mark.parametrize("name", ["idea.md", "idea_provenance.json"])
def test_project_provenance_does_not_follow_files_outside_the_project(
    tmp_path: Path,
    name: str,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("Run it.\n", encoding="utf-8")
    project = tmp_path / "project"
    idea_provenance.prepare_project_idea(source, project)
    outside = tmp_path / "outside"
    outside.write_text(project.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8")
    project.joinpath(name).unlink()
    project.joinpath(name).symlink_to(outside)

    with pytest.raises(idea_provenance.IdeaProvenanceError, match="leaves project root"):
        idea_provenance.load_project_provenance(project)


def test_a_dangling_provenance_symlink_is_not_treated_as_absent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project.joinpath("idea_provenance.json").symlink_to(tmp_path / "missing.json")

    with pytest.raises(idea_provenance.IdeaProvenanceError, match="project input is missing"):
        idea_provenance.load_project_provenance(project)


@pytest.mark.parametrize("name", ["idea.md", "idea_provenance.json"])
def test_prepare_refuses_dangling_project_input_symlinks(tmp_path: Path, name: str) -> None:
    source = tmp_path / "source.txt"
    source.write_text("Run it.\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    project.joinpath(name).symlink_to(tmp_path / "missing")

    with pytest.raises(idea_provenance.IdeaProvenanceError):
        idea_provenance.prepare_project_idea(source, project)

    assert project.joinpath(name).is_symlink()


@pytest.mark.parametrize("name", ["idea.md", "idea_provenance.json"])
def test_prepare_does_not_follow_existing_files_outside_the_project(
    tmp_path: Path,
    name: str,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("Run it.\n", encoding="utf-8")
    project = tmp_path / "project"
    idea_provenance.prepare_project_idea(source, project)
    outside = tmp_path / "outside"
    outside.write_text(project.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8")
    project.joinpath(name).unlink()
    project.joinpath(name).symlink_to(outside)

    with pytest.raises(idea_provenance.IdeaProvenanceError, match="leaves project root"):
        idea_provenance.prepare_project_idea(source, project)


def test_export_preserves_the_forge_binding_and_selected_plan(tmp_path: Path) -> None:
    forge = tmp_path / "forge.json"
    forge.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "seed_title": "Seed title",
                        "results": [
                            {
                                "b_id": "mllm_visual_tokens",
                                "b_domain": "Visual token management",
                                "idea_text": "Compress visual tokens.",
                                "plan": "Run the controlled experiment.",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "idea.txt"

    idea_provenance.export_forge_plan(forge, output, result_index=1, plan_index=1)
    artifact = idea_provenance.load_idea(output)

    assert artifact.b_id == "mllm_visual_tokens"
    assert artifact.origin == {
        "kind": "idea_forge",
        "file": "external:forge.json",
        "sha256": hashlib.sha256(forge.read_bytes()).hexdigest(),
        "result_index": 1,
        "plan_index": 1,
    }
    assert "Seed title" in artifact.body
    assert "Compress visual tokens." in artifact.body
    assert "Run the controlled experiment." in artifact.body


def test_list_forge_plans_exposes_stable_one_based_indices(tmp_path: Path) -> None:
    forge = tmp_path / "forge.json"
    forge.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "seed_title": "Seed title",
                        "results": [
                            {"b_id": "mllm_visual_tokens", "plan": "Run it"},
                            {"b_id": "llm_reasoning", "plan": "生成失败"},
                            {"b_id": "definitely_missing", "plan": "Looks complete"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert idea_provenance.list_forge_plans(forge) == [
        {
            "result_index": 1,
            "plan_index": 1,
            "seed_title": "Seed title",
            "b_id": "mllm_visual_tokens",
            "executable": True,
        },
        {
            "result_index": 1,
            "plan_index": 2,
            "seed_title": "Seed title",
            "b_id": "llm_reasoning",
            "executable": False,
        },
        {
            "result_index": 1,
            "plan_index": 3,
            "seed_title": "Seed title",
            "b_id": "definitely_missing",
            "executable": False,
        },
    ]


def test_a_directory_is_reported_as_the_wrong_kind_of_path(tmp_path: Path) -> None:
    """传目录进来时，说的是「这不是普通文件」，不是 errno 21。

    `data/idea_forge` 是目录、`data/idea_forge/forge_*.json` 才是文件，两者差一层，
    照着 `--forge` 的例子敲很容易传成前者（#214）。
    """
    with pytest.raises(idea_provenance.IdeaProvenanceError, match="not a regular file"):
        idea_provenance.list_forge_plans(tmp_path)


@pytest.mark.parametrize(
    "plan, error",
    [
        ({"b_id": "mllm_visual_tokens", "plan": "生成失败"}, "not executable"),
        ({"b_id": "definitely_missing", "plan": "Run it"}, "unknown B direction"),
    ],
)
def test_export_rejects_a_non_executable_plan(
    tmp_path: Path,
    plan: dict[str, str],
    error: str,
) -> None:
    forge = tmp_path / "forge.json"
    forge.write_text(
        json.dumps({"results": [{"seed_title": "Seed", "results": [plan]}]}),
        encoding="utf-8",
    )

    with pytest.raises(idea_provenance.IdeaProvenanceError, match=error):
        idea_provenance.export_forge_plan(forge, tmp_path / "idea.txt", 1, 1)


def test_export_refuses_to_replace_an_existing_idea(tmp_path: Path) -> None:
    forge = tmp_path / "forge.json"
    forge.write_text(
        json.dumps(
            {
                "results": [
                    {"seed_title": "Seed", "results": [{"b_id": "llm_reasoning", "plan": "Run it"}]}
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "idea.txt"
    output.write_text("Do not replace\n", encoding="utf-8")

    with pytest.raises(idea_provenance.IdeaProvenanceError, match="already exists"):
        idea_provenance.export_forge_plan(forge, output, 1, 1)


def test_export_rejects_an_out_of_range_selection(tmp_path: Path) -> None:
    forge = tmp_path / "forge.json"
    forge.write_text(
        json.dumps({"results": [{"seed_title": "Seed", "results": []}]}),
        encoding="utf-8",
    )

    with pytest.raises(idea_provenance.IdeaProvenanceError, match=r"plans index 1.*range"):
        idea_provenance.export_forge_plan(forge, tmp_path / "idea.txt", 1, 1)
