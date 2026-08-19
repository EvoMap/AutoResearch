"""Bind an executable project to the idea and B direction that produced it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from idea_forge import b_library
from plans import is_delivered

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROVENANCE_START = "<!-- autoresearch-provenance\n"
PROVENANCE_END = "\n-->"
PROVENANCE_SCHEMA_VERSION = 1
PROJECT_IDEA = "idea.md"
PROJECT_PROVENANCE = "idea_provenance.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class IdeaProvenanceError(ValueError):
    pass


@dataclass(frozen=True)
class IdeaArtifact:
    source: Path
    source_sha256: str
    body: str
    idea_sha256: str
    b_id: str | None
    origin: dict[str, object] | None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_b_id(value: object, source: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdeaProvenanceError(f"{source}: provenance b_id must be a non-empty string")
    direction = b_library.get_b_by_id(value.strip())
    if direction is None:
        raise IdeaProvenanceError(f"{source}: unknown B direction {value!r}")
    return direction["id"]


def _validate_origin(value: object, source: Path) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise IdeaProvenanceError(f"{source}: provenance origin must be a JSON object")
    expected = {"kind", "file", "sha256", "result_index", "plan_index"}
    if set(value) != expected:
        raise IdeaProvenanceError(f"{source}: provenance origin fields must be {sorted(expected)}")
    if value["kind"] != "idea_forge":
        raise IdeaProvenanceError(f"{source}: unsupported provenance origin kind {value['kind']!r}")
    if not isinstance(value["file"], str) or not value["file"]:
        raise IdeaProvenanceError(f"{source}: provenance origin file must be a non-empty string")
    if not isinstance(value["sha256"], str) or not SHA256_RE.fullmatch(value["sha256"]):
        raise IdeaProvenanceError(f"{source}: provenance origin sha256 must be a lowercase SHA256")
    for key in ("result_index", "plan_index"):
        if not isinstance(value[key], int) or isinstance(value[key], bool) or value[key] < 1:
            raise IdeaProvenanceError(f"{source}: provenance origin {key} must be a positive integer")
    return dict(value)


def _parse_metadata(
    raw: str,
    source: Path,
) -> tuple[str | None, dict[str, object] | None, str]:
    if not raw.startswith(PROVENANCE_START):
        return None, None, raw
    end = raw.find(PROVENANCE_END, len(PROVENANCE_START))
    if end < 0:
        raise IdeaProvenanceError(f"{source}: unterminated autoresearch provenance block")
    encoded = raw[len(PROVENANCE_START):end]
    try:
        metadata = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise IdeaProvenanceError(
            f"{source}: invalid JSON in provenance at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(metadata, dict):
        raise IdeaProvenanceError(f"{source}: provenance must be a JSON object")
    expected_keys = {"schema_version", "b_id", "origin"}
    unknown = sorted(set(metadata) - expected_keys)
    if unknown:
        raise IdeaProvenanceError(f"{source}: unsupported provenance field(s): {', '.join(unknown)}")
    if metadata.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise IdeaProvenanceError(
            f"{source}: provenance schema_version must be {PROVENANCE_SCHEMA_VERSION}"
        )
    b_id = _canonical_b_id(metadata.get("b_id"), source)
    origin = _validate_origin(metadata.get("origin"), source)
    body = raw[end + len(PROVENANCE_END):].lstrip("\r\n")
    return b_id, origin, body


def load_idea(path: str | Path) -> IdeaArtifact:
    source = Path(path).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise IdeaProvenanceError(f"idea file not found: {source}") from exc
    if not source.is_file():
        raise IdeaProvenanceError(f"idea path is not a regular file: {source}")
    try:
        source_bytes = source.read_bytes()
        raw = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IdeaProvenanceError(f"{source}: idea file must be UTF-8") from exc
    except OSError as exc:
        raise IdeaProvenanceError(f"{source}: cannot read idea file: {exc}") from exc
    b_id, origin, body = _parse_metadata(raw, source)
    if not body.strip():
        raise IdeaProvenanceError(f"{source}: executable idea is empty")
    return IdeaArtifact(
        source=source,
        source_sha256=_sha256(source_bytes),
        body=body,
        idea_sha256=_sha256(body.encode("utf-8")),
        b_id=b_id,
        origin=origin,
    )


def _source_name(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return f"external:{path.name}"


def _manifest_for(artifact: IdeaArtifact) -> dict[str, object]:
    manifest = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "source_file": _source_name(artifact.source),
        "source_sha256": artifact.source_sha256,
        "idea_file": PROJECT_IDEA,
        "idea_sha256": artifact.idea_sha256,
        "b_id": artifact.b_id,
        "origin": artifact.origin,
    }
    manifest["binding_sha256"] = manifest_binding_sha256(manifest)
    return manifest


def manifest_binding_sha256(payload: dict[str, object]) -> str:
    bound = {key: value for key, value in payload.items() if key != "binding_sha256"}
    encoded = json.dumps(
        bound, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256(encoded)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IdeaProvenanceError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except OSError as exc:
        raise IdeaProvenanceError(f"{path}: cannot read provenance: {exc}") from exc
    if not isinstance(payload, dict):
        raise IdeaProvenanceError(f"{path}: expected a JSON object")
    expected_keys = {
        "schema_version", "source_file", "source_sha256", "idea_file", "idea_sha256", "b_id", "origin",
        "binding_sha256",
    }
    missing = sorted(expected_keys - set(payload))
    unknown = sorted(set(payload) - expected_keys)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unsupported {', '.join(unknown)}")
        raise IdeaProvenanceError(f"{path}: invalid provenance fields: {'; '.join(details)}")
    if payload["schema_version"] != PROVENANCE_SCHEMA_VERSION:
        raise IdeaProvenanceError(
            f"{path}: schema_version must be {PROVENANCE_SCHEMA_VERSION}"
        )
    if not isinstance(payload["source_file"], str) or not payload["source_file"]:
        raise IdeaProvenanceError(f"{path}: source_file must be a non-empty string")
    if payload["idea_file"] != PROJECT_IDEA:
        raise IdeaProvenanceError(f"{path}: idea_file must be {PROJECT_IDEA!r}")
    for key in ("source_sha256", "idea_sha256", "binding_sha256"):
        if not isinstance(payload[key], str) or not SHA256_RE.fullmatch(payload[key]):
            raise IdeaProvenanceError(f"{path}: {key} must be a lowercase SHA256")
    if payload["b_id"] is not None:
        payload["b_id"] = _canonical_b_id(payload["b_id"], path)
    payload["origin"] = _validate_origin(payload["origin"], path)
    if payload["binding_sha256"] != manifest_binding_sha256(payload):
        raise IdeaProvenanceError(
            f"{path}: binding_sha256 does not match the provenance fields; restore the initialized manifest"
        )
    return payload


def _project_file(project_root: Path, name: str) -> Path:
    try:
        root = project_root.resolve(strict=True)
        path = (root / name).resolve(strict=True)
    except OSError as exc:
        raise IdeaProvenanceError(f"{project_root / name}: project input is missing: {exc}") from exc
    if not path.is_file() or not path.is_relative_to(root):
        raise IdeaProvenanceError(f"{project_root / name}: resolved path leaves project root {root}")
    return path


def load_project_provenance(project_root: str | Path) -> dict[str, object] | None:
    root = Path(project_root)
    manifest_path = root / PROJECT_PROVENANCE
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return None
    payload = _read_manifest(_project_file(root, PROJECT_PROVENANCE))
    idea_path = _project_file(root, PROJECT_IDEA)
    try:
        digest = _sha256(idea_path.read_bytes())
    except OSError as exc:
        raise IdeaProvenanceError(f"{idea_path}: cannot read materialized idea: {exc}") from exc
    if digest != payload["idea_sha256"]:
        raise IdeaProvenanceError(
            f"{idea_path}: idea_sha256 does not match {PROJECT_PROVENANCE}; "
            "restore the initialized idea or start a new project"
        )
    return payload


def prepare_project_idea(source: str | Path, project_root: str | Path) -> dict[str, object]:
    artifact = load_idea(source)
    root = Path(project_root).expanduser().resolve()
    manifest_path = root / PROJECT_PROVENANCE
    idea_path = root / PROJECT_IDEA
    expected = _manifest_for(artifact)

    if manifest_path.exists() or manifest_path.is_symlink():
        current = _read_manifest(_project_file(root, PROJECT_PROVENANCE))
        if current != expected:
            raise IdeaProvenanceError(
                f"{root}: project is already bound to a different idea; start a new project directory"
            )
        if idea_path.exists() or idea_path.is_symlink():
            safe_idea_path = _project_file(root, PROJECT_IDEA)
            if _sha256(safe_idea_path.read_bytes()) != artifact.idea_sha256:
                raise IdeaProvenanceError(
                    f"{idea_path}: project is already bound but its executable idea changed"
                )
            return current
        _atomic_write(idea_path, artifact.body)
        return current

    if idea_path.exists() or idea_path.is_symlink():
        raise IdeaProvenanceError(
            f"{idea_path}: refusing to overwrite an unmanaged project idea without {PROJECT_PROVENANCE}"
        )

    root.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write(manifest_path, encoded)
    _atomic_write(idea_path, artifact.body)
    return expected


def inspect_idea(source: str | Path) -> dict[str, object]:
    artifact = load_idea(source)
    preview = re.sub(r"\s+", " ", artifact.body).strip()[:80]
    return {
        "idea_file": str(artifact.source),
        "idea_preview": preview,
        "idea_sha256": artifact.idea_sha256,
        "source_sha256": artifact.source_sha256,
        "b_id": artifact.b_id,
        "origin": artifact.origin,
    }


def _load_forge(path: str | Path) -> tuple[Path, bytes, dict[str, object]]:
    source = Path(path).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise IdeaProvenanceError(f"forge file not found: {source}") from exc
    # 先判种类再读。放在读之后，这句话永远说不出口：传目录进来 read_bytes 就抛
    # IsADirectoryError，被下面的 OSError 接住报成「读不了」（#214）。load_idea 的顺序是对的。
    if not source.is_file():
        raise IdeaProvenanceError(f"forge path is not a regular file: {source}")
    try:
        raw = source.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise IdeaProvenanceError(f"{source}: forge file must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise IdeaProvenanceError(
            f"{source}: invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except OSError as exc:
        raise IdeaProvenanceError(f"{source}: cannot read forge file: {exc}") from exc
    if not isinstance(payload, dict):
        raise IdeaProvenanceError(f"{source}: forge output must be a JSON object")
    return source, raw, payload


def _one_based(items: object, index: int, label: str, source: Path) -> dict[str, object]:
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise IdeaProvenanceError(f"{source}: {label} must be a list of objects")
    if not isinstance(index, int) or isinstance(index, bool) or index < 1 or index > len(items):
        raise IdeaProvenanceError(
            f"{source}: {label} index {index} is out of range 1..{len(items)}"
        )
    return items[index - 1]


def _forge_idea_body(record: dict[str, object], plan: dict[str, object]) -> str:
    title = str(record.get("seed_title") or plan.get("seed_title") or "Exported Idea Forge plan").strip()
    idea = str(plan.get("idea_text") or "").strip()
    execution_plan = str(plan.get("plan") or "").strip()
    if not is_delivered(execution_plan):
        raise IdeaProvenanceError("selected Idea Forge plan is not executable")
    sections = [f"# {title}"]
    if idea:
        sections.extend(("## 1. Research idea", idea))
    sections.extend(("## 2. Execution plan", execution_plan))
    return "\n\n".join(sections) + "\n"


def list_forge_plans(forge_file: str | Path) -> list[dict[str, object]]:
    source, _, payload = _load_forge(forge_file)
    results = payload.get("results")
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise IdeaProvenanceError(f"{source}: results must be a list of objects")
    listed = []
    for result_index, record in enumerate(results, start=1):
        plans = record.get("results")
        if not isinstance(plans, list) or any(not isinstance(item, dict) for item in plans):
            raise IdeaProvenanceError(f"{source}: results[{result_index}].results must be a list of objects")
        for plan_index, plan in enumerate(plans, start=1):
            plan_text = plan.get("plan")
            b_id = plan.get("b_id")
            valid_direction = isinstance(b_id, str) and b_library.get_b_by_id(b_id) is not None
            executable = isinstance(plan_text, str) and is_delivered(plan_text) and valid_direction
            listed.append(
                {
                    "result_index": result_index,
                    "plan_index": plan_index,
                    "seed_title": str(record.get("seed_title") or ""),
                    "b_id": b_id,
                    "executable": executable,
                }
            )
    return listed


def export_forge_plan(
    forge_file: str | Path,
    output: str | Path,
    result_index: int,
    plan_index: int,
) -> Path:
    source, raw, payload = _load_forge(forge_file)
    record = _one_based(payload.get("results"), result_index, "results", source)
    plan = _one_based(record.get("results"), plan_index, "plans", source)
    b_id = _canonical_b_id(plan.get("b_id"), source)
    body = _forge_idea_body(record, plan)
    origin = {
        "kind": "idea_forge",
        "file": _source_name(source),
        "sha256": _sha256(raw),
        "result_index": result_index,
        "plan_index": plan_index,
    }
    metadata = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "b_id": b_id,
        "origin": origin,
    }
    target = Path(output).expanduser()
    if target.exists() or target.is_symlink():
        raise IdeaProvenanceError(f"{target}: output already exists; choose a new idea file")
    encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    _atomic_write(target, f"{PROVENANCE_START}{encoded}{PROVENANCE_END}\n\n{body}")
    load_idea(target)
    return target.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and materialize an AutoResearch idea")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="validate an idea without writing files")
    inspect_parser.add_argument("--idea-file", type=Path, required=True)
    prepare_parser = subparsers.add_parser("prepare", help="bind an idea to a project directory")
    prepare_parser.add_argument("--idea-file", type=Path, required=True)
    prepare_parser.add_argument("--project-root", type=Path, required=True)
    export_parser = subparsers.add_parser("export", help="export one executable Idea Forge plan")
    export_parser.add_argument("--forge-file", type=Path, required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--result-index", type=int, required=True, help="one-based seed result index")
    export_parser.add_argument("--plan-index", type=int, required=True, help="one-based plan index within the seed")
    list_parser = subparsers.add_parser("list", help="list plans and their one-based indices")
    list_parser.add_argument("--forge-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_idea(args.idea_file)
        elif args.command == "prepare":
            result = prepare_project_idea(args.idea_file, args.project_root)
        elif args.command == "export":
            output = export_forge_plan(
                args.forge_file, args.output, args.result_index, args.plan_index
            )
            result = {"output": str(output)}
        else:
            result = {"plans": list_forge_plans(args.forge_file)}
    except IdeaProvenanceError as exc:
        print(f"idea provenance error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
