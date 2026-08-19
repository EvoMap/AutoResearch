"""配置统一的第一道门，它自己得先能拦住东西。

门的价值全在拒绝什么，所以这里的重点是四条反例：新增一个未覆盖的模型名要红、新增一个
未覆盖的 env 要红、清单里留一条已经覆盖了的也要红、以及提示词和注释里的模型名不算数。

最后一条是这道门最初的设计错误：第一版打算扫「源码里出现的模型名字面量」，而
`llm_client.py` 里那种形状的字面量有十几处，混着提示词、注释和像 `"claude"` 这样的路由
分支标记。按字面量扫会产出一份带误报的清单，而按这个仓库自己的经验（`ruff.toml` 里那
段），没人能弄绿的门会被关掉。所以它扫的是调用点。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "check_model_references.py"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import check_model_references as gate  # noqa: E402
import providers  # noqa: E402


def run(cwd=None):
    return subprocess.run([sys.executable, str(GATE)], capture_output=True, text=True,
                          cwd=str(cwd or REPO), timeout=120)


def test_the_gate_is_green_on_the_current_tree():
    """今天必须绿，否则它进不了 CI，进不了 CI 就挡不住任何东西。"""
    done = run()
    assert done.returncode == 0, done.stdout


def test_a_new_uncovered_model_turns_it_red(monkeypatch, capsys):
    """在现有引用之上加一个，而不是替换掉全部。

    替换会让清单里每一条都变陈旧，于是门因为 stale 而红：测试通过，但通过的原因不是
    要测的那个。
    """
    current = gate.model_references()
    monkeypatch.setattr(gate, "model_references",
                        lambda: {**current, "brand-new-model": ["src/x.py:1"]})

    assert gate.main() == 1
    out = capsys.readouterr().out
    assert "新增了未覆盖的引用" in out and "model:brand-new-model" in out
    assert "请从 EXPECTED_UNRESOLVED 里删掉" not in out, "红的原因应该是新增，不是陈旧"


def test_a_new_uncovered_env_turns_it_red(monkeypatch, capsys):
    current = gate.env_references()
    monkeypatch.setattr(gate, "env_references",
                        lambda: {**current, "BRAND_NEW_ENV": ["scripts/x.ts:1"]})

    assert gate.main() == 1
    out = capsys.readouterr().out
    assert "env:BRAND_NEW_ENV" in out
    assert "请从 EXPECTED_UNRESOLVED 里删掉" not in out


def test_a_new_uncovered_role_turns_it_red(monkeypatch, capsys):
    current = gate.role_references()
    monkeypatch.setattr(gate, "role_references",
                        lambda: {**current, "brand_new_role": ["scripts/x.ts:1"]})

    assert gate.main() == 1
    out = capsys.readouterr().out
    assert "role:brand_new_role" in out
    assert "请从 EXPECTED_UNRESOLVED 里删掉" not in out


def test_a_stale_exemption_turns_it_red(monkeypatch):
    """清单只允许变短。留一条已经覆盖了的，它就慢慢变成没人维护的免罪符。

    这条不是假想：第一版手写的清单里有 5 条早已被配置覆盖，是这道门自己抓出来的。
    """
    monkeypatch.setattr(gate, "EXPECTED_UNRESOLVED",
                        gate.EXPECTED_UNRESOLVED | {"model:already-covered"})

    assert gate.main() == 1


def test_a_model_name_outside_a_call_site_is_not_counted():
    """按字面量扫会把这些算进来，按调用点扫不会。"""
    names = gate.model_references()
    scanned = "\n".join(path.read_text(encoding="utf-8") for path in gate.PY_SOURCES)

    # 提示词里点名的基线模型：源码里有，调用点里没有。
    assert "Qwen2.5-VL" in scanned, "取样的字面量不在被扫文件里，这条断言没意义"
    assert not any(name.startswith("Qwen") for name in names)

    # provider 方言是运行分支标记，不是模型标识。
    assert '"anthropic_gateway"' in scanned
    assert "anthropic_gateway" not in names


def test_it_finds_the_call_sites_that_matter():
    """反方向的负控：别为了没有误报而漏掉真正的调用点。"""
    forge = (REPO / "src" / "idea_forge" / "forge.py").read_text(encoding="utf-8")
    assert 'configured_distinct_role_models("ideator")' in forge
    assert 'call_role("planner"' in forge

    reviewer = (REPO / "ar-runtime" / "scripts" / "ar-gemini-review-mcp.ts").read_text(encoding="utf-8")
    critic = (REPO / "ar-runtime" / "scripts" / "ar-external-critic-mcp.ts").read_text(encoding="utf-8")
    assert re.search(r"callRole\(\s*'code_reviewer'", reviewer)
    assert re.search(r"safeCallRole\(\s*'critic'", critic)
    assert {"critic", "critic_secondary"} <= set(gate.role_references())
    assert not gate.env_references(), "MCP 不应再自己读取 provider 专用 env"


def test_the_backstop_table_is_seen():
    """模型名从 forge 搬进 ROLE_DEFAULTS 之后（#190），Python 侧只剩这一处写着模型名。

    漏扫它，门会因为自己瞎了而绿：源码里还有六个写死的模型名，而报告说零。
    """
    import llm_client

    names = gate.model_references()
    for role, recommended in llm_client.ROLE_DEFAULTS.items():
        for model in ([recommended] if isinstance(recommended, str) else recommended):
            assert any("llm_client.py" in place for place in names.get(model, [])), \
                f"{role} 的兜底模型 {model} 没被扫到"


def test_the_forge_no_longer_names_a_model():
    """席位来自配置，所以构思那两个模块里不该再出现模型名（#190）。"""
    names = gate.model_references()
    named_in_forge = {model for model, places in names.items()
                      if any("idea_forge/" in place for place in places)}

    assert not named_in_forge, f"这些模型名又被写回构思模块了：{sorted(named_in_forge)}"


def test_the_burn_down_list_says_which_step_clears_each_entry():
    """一份没写「谁来清」的欠债清单，就是一份永远不会被清的清单。"""
    source = GATE.read_text(encoding="utf-8")
    body = source[source.index("EXPECTED_UNRESOLVED = {"):source.index("def declared()")]

    for entry in gate.EXPECTED_UNRESOLVED:
        assert entry in body, f"{entry} 不在带注释的那份清单里"
    assert "Step 2" in body


@pytest.mark.parametrize("path", [
    REPO / "src" / "llm_client.py",
    REPO / "src" / "idea_forge" / "forge.py",
    REPO / "ar-runtime" / "scripts" / "ar-gemini-review-mcp.ts",
])
def test_the_scanned_files_still_exist(path):
    """扫一个不存在的文件会静默少扫一批调用点，而门照样绿。"""
    assert path.exists(), f"{path} 不在了，check_model_references 的清单要更新"


def test_every_declared_profile_name_is_usable_as_a_role_candidate():
    """配置里的 profile 如果没有任何角色引用它，它不是真相的一部分，是遗留。"""
    config = json.loads((REPO / "config" / "providers.example.json").read_text(encoding="utf-8"))
    profiles = set(providers.as_profiles(config))
    referenced = {name for role in config["roles"]
                  for name in providers.role_candidates(config, role)}

    assert not (profiles - referenced), f"这些 profile 没有任何角色用到：{profiles - referenced}"
