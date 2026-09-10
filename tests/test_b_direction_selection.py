"""一次运行用哪些 知识方向。

B_LIBRARY 是默认注册表，knowledge_base/ 还包含更多文档。一份文档被读，是因为
它被点了名——注册在库里，或者写进 config 的 idea_forge.b_directions。其余文档不会被读，所以
放着不花钱。

三个入口能要方向：idea_generation 的日常跑、直接调 run_idea_forge(b_ids=...)、pending 补跑。它们
曾经各有一套语义，这里的用例钉住它们给出同一个答案。

边界（名字能指到哪个文件）在 test_knowledge_path_boundary.py；闸门判不了时怎么记在
test_consensus_gate.py。"""

from __future__ import annotations

import builtins
import json
import re
import sys
import types

import pytest

from conftest import REPO


def test_naming_a_knowledge_file_makes_the_system_read_it(forge, tmp_path):
    """B_LIBRARY is the registry; the directory holds more documents than it lists.

    Before this, using one of those meant editing Python. Naming it in the config
    is enough now: the id is the filename, the domain comes from the H1, and the
    curated datasets and baselines are simply absent -- freshness.py already falls
    back to an arxiv search when they are.
    """
    bl = forge.bl
    (tmp_path / "my_new_domain.md").write_text("# My New Domain\n\n内容\n", encoding="utf-8")
    bl.KNOWLEDGE_BASE_DIR = tmp_path

    selected, reason = bl.select_b_directions(["my_new_domain"])
    assert [b["id"] for b in selected] == ["my_new_domain"]
    assert selected[0]["domain"] == "My New Domain"
    assert selected[0]["datasets"] == []
    assert "b_ids" in reason, "the report should name where the choice came from"

def test_the_registry_is_the_default(forge, tmp_path):
    """A directory full of documents must not change what a plain run does."""
    bl = forge.bl
    for name in ("extra_one.md", "extra_two.md"):
        (tmp_path / name).write_text("# X\n", encoding="utf-8")
    bl.KNOWLEDGE_BASE_DIR = tmp_path

    selected, _ = bl.select_b_directions()
    assert {b["id"] for b in selected} == {b["id"] for b in bl.B_LIBRARY}

def test_config_overrides_the_files(forge):
    bl = forge.bl
    stub = forge.llm
    stub.load_config = lambda: {"idea_forge": {"b_directions": ["llm_reasoning"]}}

    selected, reason = bl.select_b_directions()
    assert [b["id"] for b in selected] == ["llm_reasoning"]
    assert "config" in reason

def test_a_name_with_no_entry_and_no_file_is_an_error(forge, stub_free=None):
    bl = forge.bl
    stub = forge.llm
    stub.load_config = lambda: {"idea_forge": {"b_directions": ["no_such_domain"]}}

    with pytest.raises(ValueError, match="no_such_domain"):
        bl.select_b_directions()

def test_an_empty_knowledge_base_still_runs(forge, tmp_path):
    """A fresh clone has no knowledge files and must not end up with zero directions."""
    bl = forge.bl
    bl.KNOWLEDGE_BASE_DIR = tmp_path

    selected, _ = bl.select_b_directions()
    assert len(selected) == len(bl.B_LIBRARY)

def test_a_bare_string_is_one_name_not_a_silent_fallback(forge):
    """`"b_directions": "agent_memory"` is the obvious way to write one entry.

    It used to fall through to the registry with no message, so a user who asked
    for one direction quietly got four. A string is also iterable, so accepting it
    as a list would have asked for the directions a, g, e, n...
    """
    bl = forge.bl
    stub = forge.llm
    stub.load_config = lambda: {"idea_forge": {"b_directions": "agent_memory"}}

    selected, _ = bl.select_b_directions()
    assert [b["id"] for b in selected] == ["agent_memory"]

def test_a_b_directions_that_is_not_a_list_is_an_error(forge):
    bl = forge.bl
    stub = forge.llm
    stub.load_config = lambda: {"idea_forge": {"b_directions": {"id": "agent_memory"}}}

    with pytest.raises(ValueError, match="b_directions"):
        bl.select_b_directions()

def test_a_repeated_name_is_only_run_once(forge):
    """Each direction costs one ideation and one cross-review round per seed."""
    bl = forge.bl
    selected, _ = bl.select_b_directions(["agent_memory", "llm_reasoning", "agent_memory"])
    assert [b["id"] for b in selected] == ["agent_memory", "llm_reasoning"]

def test_writing_the_filename_still_gets_the_registered_entry(forge):
    """A regression from widening knowledge_path() to accept both spellings.

    `agent_memory.md` used to be rejected outright. Once the path helper accepted
    a suffix, it resolved -- but get_b_by_id matched the registry on the bare id,
    so the name fell through to direction_from_file and produced a direction with
    no datasets or baselines. Quieter than an error and weaker than the entry the
    user was asking for.
    """
    bl = forge.bl
    by_stem, _ = bl.select_b_directions(["agent_memory"])
    by_file, _ = bl.select_b_directions(["agent_memory.md"])
    assert by_file == by_stem
    assert by_file[0]["datasets"], "the curated metadata freshness.py reads"

def test_the_two_spellings_are_one_direction(forge):
    """Otherwise the same domain is ideated and cross-reviewed twice per seed."""
    bl = forge.bl
    selected, _ = bl.select_b_directions(["agent_memory", "agent_memory.md"])
    assert [b["id"] for b in selected] == ["agent_memory"]

def test_a_broken_config_fails_instead_of_looking_unconfigured(tmp_path, monkeypatch):
    """A JSONDecodeError was caught and read as "the user did not configure this".

    The run then quietly used the registry's four, and the same broken file blew
    up later at the first model call, by which point the error is nowhere near its
    cause. Uses a real config file rather than a stub raising on demand -- the
    point is which exceptions the real read path produces.
    """
    monkeypatch.delitem(sys.modules, "llm_client", raising=False)
    __import__("llm_client")

    import idea_forge.b_library as bl

    broken = tmp_path / "providers.local.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("AUTORESEARCH_CONFIG", str(broken))

    with pytest.raises(json.JSONDecodeError):
        bl.select_b_directions()

def test_a_broken_dependency_is_not_a_missing_llm_client(forge, monkeypatch):
    """ModuleNotFoundError covers "llm_client needs a package you do not have".

    Catching every ImportError read that as "there is no config to read" and the
    run used the registry's four. Only llm_client itself being absent means that
    -- which is the case when b_library.py is run directly, since sys.path then
    holds src/idea_forge rather than src.
    """
    bl = forge.bl
    real_import = builtins.__import__

    def missing_httpx(name, *args, **kwargs):
        if name == "llm_client":
            raise ModuleNotFoundError("No module named 'httpx'", name="httpx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_httpx)
    with pytest.raises(ModuleNotFoundError, match="httpx"):
        bl.select_b_directions()

def test_a_selected_direction_survives_into_the_forge(forge, tmp_path):
    """The selection and the forge have to resolve a name the same way.

    run_idea_forge takes b_ids and filtered them against B_LIBRARY, so a direction
    that only exists as a knowledge file was accepted by the selection, printed as
    chosen, and then dropped with no message -- the user sees a domain they asked
    for produce nothing.
    """
    bl = forge.bl
    (tmp_path / "Coding_Agent.md").write_text("# Coding Agents\n", encoding="utf-8")
    bl.KNOWLEDGE_BASE_DIR = tmp_path

    import idea_forge.forge as forge
    selected, _ = bl.select_b_directions(["agent_memory", "Coding_Agent"])

    resolved = forge.resolve_directions([b["id"] for b in selected])

    assert [b["id"] for b in resolved] == ["agent_memory", "Coding_Agent"]

def test_the_forge_with_no_b_ids_reads_the_config(forge):
    """It used to return the registry, ignoring a config the daily run honoured."""
    bl = forge.bl
    stub = forge.llm
    import idea_forge.forge as forge

    assert forge.resolve_directions() == list(bl.B_LIBRARY)

    stub.load_config = lambda: {"idea_forge": {"b_directions": ["llm_reasoning"]}}
    assert [b["id"] for b in forge.resolve_directions()] == ["llm_reasoning"]

def test_the_forge_rejects_an_unknown_id_like_the_config_does(forge):
    """run_idea_forge(b_ids=...) is reachable without going through the config."""
    import idea_forge.forge as forge
    with pytest.raises(ValueError, match="definitely_not_a_direction"):
        forge.resolve_directions(["definitely_not_a_direction"])

def test_the_forge_dedupes_like_the_config_does(forge):
    import idea_forge.forge as forge
    got = forge.resolve_directions(["agent_memory", "agent_memory"])
    assert [b["id"] for b in got] == ["agent_memory"]

def test_the_forge_takes_a_bare_string_like_the_config_does(forge):
    import idea_forge.forge as forge
    assert [b["id"] for b in forge.resolve_directions("agent_memory")] == ["agent_memory"]

def test_the_pending_rerun_uses_the_configured_directions(forge, monkeypatch, tmp_path):
    """A normal run honoured the config and the catch-up run did not.

    run_pending_forge read get_b_library() directly, so seeds deferred from a run
    that used the configured directions came back through the registry's four.
    """
    stub = forge.llm
    stub.load_config = lambda: {"idea_forge": {"b_directions": ["llm_reasoning"]}}

    sys.path.insert(0, str(REPO))
    import run_pending_forge as rpf

    pending = tmp_path / "pending_forge_seeds.json"
    pending.write_text(json.dumps({"seeds": [{"title": "s"}], "count": 1}), encoding="utf-8")
    monkeypatch.setattr(rpf, "PENDING_FILE", pending)
    monkeypatch.setattr(rpf, "log", lambda *a: None)

    seen = {}
    forge_stub = types.ModuleType("idea_forge.forge")
    forge_stub.run_idea_forge = lambda seeds, b_ids=None: seen.update(b_ids=b_ids) or {"summary": {}}
    monkeypatch.setitem(sys.modules, "idea_forge.forge", forge_stub)
    dashboard_stub = types.ModuleType("generate_dashboard")
    dashboard_stub.generate_html = lambda: None
    monkeypatch.setitem(sys.modules, "generate_dashboard", dashboard_stub)
    idea_page_stub = types.ModuleType("generate_idea_page")
    idea_page_stub.generate = lambda: None
    monkeypatch.setitem(sys.modules, "generate_idea_page", idea_page_stub)
    published = []
    monkeypatch.setattr(rpf, "publish_generated_pages", lambda: published.append(True))

    rpf.main()

    assert seen["b_ids"] == ["llm_reasoning"]
    assert json.loads(pending.read_text())["seeds"] == [], "the queue is drained after a run"
    assert published == [True]


@pytest.mark.parametrize("document", ["README.md", "README_CN.md"])
def test_readme_direction_example_uses_bundled_knowledge(forge, document):
    text = (REPO / document).read_text(encoding="utf-8")
    examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.S)
                if '"b_directions"' in block]
    assert examples, f"{document} must include a direction configuration example"
    for config in examples:
        forge.llm.load_config = lambda: config
        selected, _ = forge.bl.select_b_directions()
        assert selected
        for direction in selected:
            assert forge.bl.knowledge_path(direction["id"]) is not None
