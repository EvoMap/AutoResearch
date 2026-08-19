from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "ar-runtime" / ".claude" / "skills" / "ar-coordinator" / "SKILL.md"


def test_coordinator_uses_the_idea_provenance_entrypoint_before_monitor_start() -> None:
    source = SKILL.read_text(encoding="utf-8")

    inspect = "python ../src/idea_provenance.py inspect"
    prepare = "python ../src/idea_provenance.py prepare"
    monitor = "立刻启动 monitor"
    assert inspect in source
    assert prepare in source
    assert '--idea-file "<idea_file>"' in source
    assert '--project-root "<project_root>"' in source
    assert "idea         = <idea_preview 前 60 字>" in source
    assert "idea         = <idea_text 前 60 字>" not in source
    assert source.index(prepare) < source.index(monitor)


def test_coordinator_records_the_machine_owned_idea_artifacts() -> None:
    source = SKILL.read_text(encoding="utf-8")

    assert "idea_artifact: <project_root>/idea.md" in source
    assert "idea_provenance: <project_root>/idea_provenance.json" in source
