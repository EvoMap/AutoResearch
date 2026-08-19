"""投影分两层，因为它们的风险面不一样。

CI 看不到 ignored 文件（`settings.local.json` 含真实凭据），所以「重新生成并比对」这道门
只能架在 tracked 的那层。两层混成一个门的话，要么门永远红、要么它其实什么都没比。

这里主要测它**不碰**什么：`settings.local.json` 里 `permissions`、`mcpServers`、
`enabledPlugins` 都不是生成的，覆盖整个文件是最容易把别人机器搞坏的一件事。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import render_env  # noqa: E402

CONFIG = json.loads((REPO / "config" / "providers.example.json").read_text(encoding="utf-8"))


def test_the_tracked_template_stays_in_sync_with_the_config():
    """这道门就是 tracked 那层的全部意义。"""
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "render_env.py"), "--check"],
                          capture_output=True, text=True, timeout=120)

    assert done.returncode == 0, done.stdout + done.stderr


def test_the_template_never_gets_a_shell_style_placeholder():
    """The official CLI treats `${VAR}` in settings JSON as a literal value."""
    body = (REPO / "ar-runtime" / ".claude" / "settings.local.example.json").read_text(
        encoding="utf-8")

    assert "${" not in body


def test_only_model_keys_are_owned_in_the_tracked_template():
    """端点和凭据留给人填；模板只负责说清填什么。"""
    plan = render_env.detect_conflicts(render_env.claims(CONFIG))
    current = json.loads(
        (REPO / "ar-runtime" / ".claude" / "settings.local.example.json").read_text("utf-8"))
    rendered = render_env.render_example(plan, current)

    for key, entry in plan.items():
        if entry["axis"] in ("model", "request_max_tokens"):
            assert rendered["env"][key] == entry["value"]
        else:
            assert rendered["env"][key] == current["env"][key], f"{key} 不该被模板接管"


def test_nothing_outside_the_owned_keys_is_touched():
    plan = render_env.detect_conflicts(render_env.claims(CONFIG))
    current = json.loads(
        (REPO / "ar-runtime" / ".claude" / "settings.local.example.json").read_text("utf-8"))

    rendered = render_env.render_example(plan, current)

    assert rendered["permissions"] == current["permissions"]
    assert rendered["mcpServers"] == current["mcpServers"]
    assert rendered.get("enabledPlugins") == current.get("enabledPlugins")
    assert not (set(rendered["env"]) & set(render_env.RETIRED_OWNED))
    for key, value in current["env"].items():
        if key not in render_env.OWNED:
            assert rendered["env"][key] == value


def test_a_hand_edited_owned_key_is_refused_without_force(tmp_path, monkeypatch):
    """本机手改过的托管键不能被悄悄覆盖。"""
    local = tmp_path / "settings.local.json"
    local.write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "my-own-choice"},
                                 "permissions": {"allow": ["Bash(*)"]}}), encoding="utf-8")
    monkeypatch.setattr(render_env, "SETTINGS_LOCAL", local)

    with pytest.raises(render_env.Conflict) as exc:
        render_env.materialise(render_env.detect_conflicts(render_env.claims(CONFIG)), {}, force=False)

    assert "ANTHROPIC_MODEL" in str(exc.value) and "--force" in str(exc.value)


def test_force_overwrites_but_still_keeps_the_rest(tmp_path, monkeypatch):
    local = tmp_path / "settings.local.json"
    local.write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "my-own-choice", "NO_PROXY": "x"},
                                 "permissions": {"allow": ["Bash(*)"]}}), encoding="utf-8")
    monkeypatch.setattr(render_env, "SETTINGS_LOCAL", local)

    result, changed = render_env.materialise(render_env.detect_conflicts(render_env.claims(CONFIG)), {}, force=True)

    assert result["env"]["NO_PROXY"] == "x"
    assert result["permissions"] == {"allow": ["Bash(*)"]}
    assert "ANTHROPIC_MODEL" in changed


def test_the_local_file_is_written_atomically_and_locked_down(tmp_path):
    target = tmp_path / "nested" / "settings.local.json"

    render_env.write_atomically(target, '{"env": {}}\n')

    assert target.read_text(encoding="utf-8") == '{"env": {}}\n'
    if os.name != "nt":
        assert oct(target.stat().st_mode)[-3:] == "600", "含凭据的文件不能是 world-readable"
    assert not [p for p in target.parent.iterdir() if p.name != target.name], "没留下临时文件"


def test_retired_generated_provider_keys_are_removed(tmp_path, monkeypatch):
    local = tmp_path / "settings.local.json"
    local.write_text(json.dumps({
        "env": {"GEMINI_API_KEY": "generated-old-value", "NO_PROXY": "localhost"},
        "_generated_from": {"values": {"GEMINI_API_KEY": "generated-old-value"}},
    }), encoding="utf-8")
    monkeypatch.setattr(render_env, "SETTINGS_LOCAL", local)

    result, _ = render_env.materialise(render_env.detect_conflicts(render_env.claims(CONFIG)), {}, force=False)

    assert "GEMINI_API_KEY" not in result["env"]
    assert result["env"]["NO_PROXY"] == "localhost"
    assert "GEMINI_API_KEY" not in result["_generated_from"]["values"]


def test_retired_hand_written_provider_keys_require_confirmation(tmp_path, monkeypatch):
    local = tmp_path / "settings.local.json"
    local.write_text(
        json.dumps({"env": {"GEMINI_API_KEY": "hand-written"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(render_env, "SETTINGS_LOCAL", local)

    with pytest.raises(render_env.Conflict) as exc:
        render_env.materialise(
            render_env.detect_conflicts(render_env.claims(CONFIG)), {}, force=False)

    assert "GEMINI_API_KEY" in str(exc.value) and "--force" in str(exc.value)


def test_retired_keys_make_a_projection_stale(tmp_path, monkeypatch):
    local = tmp_path / "settings.local.json"
    local.write_text(json.dumps({
        "env": {"GEMINI_API_KEY": "stale"},
        "_generated_from": {"values": {}},
    }), encoding="utf-8")
    monkeypatch.setattr(render_env, "SETTINGS_LOCAL", local)

    fresh, reason = render_env.local_is_fresh()

    assert fresh is False
    assert "GEMINI_API_KEY" in reason


def test_projection_only_owns_the_claude_code_main_loop():
    """Reviewer/critic read unified JSON directly and need no provider-specific projection."""
    plan = render_env.detect_conflicts(render_env.claims(CONFIG))

    assert set(plan) == {
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    }
    assert not any(key.startswith(("GEMINI_", "GPT_CRITIC_")) for key in plan)


def test_no_credential_value_reaches_the_tracked_template(monkeypatch):
    """模板进 git，绝不能带上环境里的真值。"""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-super-secret-value")
    plan = render_env.detect_conflicts(render_env.claims(CONFIG))
    current = json.loads(
        (REPO / "ar-runtime" / ".claude" / "settings.local.example.json").read_text("utf-8"))

    rendered = json.dumps(render_env.render_example(plan, current))

    assert "sk-super-secret-value" not in rendered


def test_a_config_upgrade_is_not_mistaken_for_a_hand_edit(tmp_path, monkeypatch):
    """记下上次写出去的值，配置正常升级就不用 --force。

    没有它的时候，任何差异都被当成手改：升一次配置就得 --force，而 --force 会连真正的
    手改一起盖掉。那正好把这道保护变成人人绕过的一步。
    """
    local = tmp_path / "settings.local.json"
    monkeypatch.setattr(render_env, "SETTINGS_LOCAL", local)

    first, _ = render_env.materialise(
        render_env.detect_conflicts(render_env.claims(CONFIG)), {}, force=False)
    local.write_text(json.dumps(first), encoding="utf-8")
    assert first["_generated_from"]["values"] == first["env"]

    # 模拟配置升级：模型名换了，但上一次确实是投影生成的。
    plan = render_env.detect_conflicts(render_env.claims(CONFIG))
    plan["ANTHROPIC_MODEL"] = {**plan["ANTHROPIC_MODEL"], "value": "a-newer-model"}

    updated, changed = render_env.materialise(plan, {}, force=False)

    assert updated["env"]["ANTHROPIC_MODEL"] == "a-newer-model"
    assert "ANTHROPIC_MODEL" in changed


def test_a_hand_written_file_still_needs_force(tmp_path, monkeypatch):
    """没有生成指纹的文件就是人写的，不能悄悄盖掉。"""
    local = tmp_path / "settings.local.json"
    local.write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "mine"}}), encoding="utf-8")
    monkeypatch.setattr(render_env, "SETTINGS_LOCAL", local)

    with pytest.raises(render_env.Conflict):
        render_env.materialise(
            render_env.detect_conflicts(render_env.claims(CONFIG)), {}, force=False)


def test_bringup_can_tell_whether_the_local_projection_is_current(tmp_path, monkeypatch):
    """本机那份含真实凭据，CI 看不见，所以新鲜度只能在本机查。"""
    local = tmp_path / "settings.local.json"
    monkeypatch.setattr(render_env, "SETTINGS_LOCAL", local)
    plan = render_env.detect_conflicts(render_env.claims(render_env.load_config("local")))

    assert render_env.local_is_fresh() == (False, "还没生成过，跑 python scripts/render_env.py")

    generated, _ = render_env.materialise(plan, {}, force=False)
    local.write_text(json.dumps(generated), encoding="utf-8")
    assert render_env.local_is_fresh({})[0] is True

    local.write_text(json.dumps({"env": {}, "_generated_from": {"values": {}}}),
                     encoding="utf-8")
    fresh, why = render_env.local_is_fresh({})
    assert fresh is False and "重新生成" in why


def test_upgrading_the_code_alone_makes_the_projection_stale(tmp_path, monkeypatch):
    """升级改的是生成逻辑，配置一个字节没动。

    判据要是一份存下来的配置指纹，这里就报 fresh，而官方 CLI 读的
    `settings.local.json` 已经不是现在会写出去的那份。#185 改的正是两侧共用的候选判据，
    v0.1.0 之后的升级就命中这个形状（#219）。
    """
    local = tmp_path / "settings.local.json"
    monkeypatch.setattr(render_env, "SETTINGS_LOCAL", local)
    plan = render_env.detect_conflicts(render_env.claims(render_env.load_config("local")))
    generated, _ = render_env.materialise(plan, {}, force=False)
    local.write_text(json.dumps(generated), encoding="utf-8")
    assert render_env.local_is_fresh({})[0] is True

    # 只换生成逻辑：配置、环境和文件本身都不动。
    real = render_env.claims
    monkeypatch.setattr(render_env, "claims", lambda *a, **kw: [
        {**c, "value": "a-model-the-new-code-picks"} if c["axis"] == "model" else c
        for c in real(*a, **kw)])

    fresh, why = render_env.local_is_fresh({})

    assert fresh is False and "ANTHROPIC_MODEL" in why


def test_the_local_layer_resolves_what_preflight_resolves():
    """两层各读各的输入，但本机那层必须和 preflight 看到的是同一份配置。

    上一版两层都固定读 tracked 的 example，而 preflight 读的是 providers.local.json
    合并上 tracked 默认值。本机实测两条路径解析出的 10 个托管键**全部**不一致：投影会
    把官方 CLI 的设置写成示例里的 route，preflight 却在检查本地那条。
    """
    import providers

    effective, _, _ = providers.load_effective_config(REPO)
    from_projection = render_env.load_config("local")

    assert from_projection == effective


def test_the_tracked_layer_never_leaks_a_local_endpoint_name():
    """模板要发布，所以它只能由 tracked 配置生成。

    用生效配置生成的话，这台机器的端点变量名（GATEWAY_BASE、AZURE_OPENAI_ENDPOINT
    之类）会随模板进 git。
    """
    tracked = render_env.load_config("tracked")

    assert tracked == json.loads(render_env.CONFIG.read_text(encoding="utf-8"))


def test_a_hand_edit_after_generation_is_not_silently_overwritten(tmp_path, monkeypatch):
    """只比配置哈希不够：生成后手改，哈希没变，于是「手改」被当成「一致」。"""
    local = tmp_path / "settings.local.json"
    monkeypatch.setattr(render_env, "SETTINGS_LOCAL", local)
    plan = render_env.detect_conflicts(render_env.claims(CONFIG))

    generated, _ = render_env.materialise(plan, {}, force=False)
    local.write_text(json.dumps(generated), encoding="utf-8")

    # 有人手改了一个托管键。配置没动，所以哈希还是一样的。
    edited = json.loads(local.read_text(encoding="utf-8"))
    edited["env"]["ANTHROPIC_MODEL"] = "my-own-choice"
    local.write_text(json.dumps(edited), encoding="utf-8")

    fresh, why = render_env.local_is_fresh()
    assert fresh is False and "ANTHROPIC_MODEL" in why, "手改要被看见"

    with pytest.raises(render_env.Conflict) as exc:
        render_env.materialise(plan, {}, force=False)
    assert "my-own-choice" in str(exc.value)


# ---- 覆盖要在投影这一侧也生效 ----

def test_a_role_override_reaches_the_projection(monkeypatch):
    """The official CLI reads projected settings.local.json, not provider config directly.

    投影原来直接读配置的候选，绕过了 `AR_MODEL_<ROLE>`：设了 AR_MODEL_AGENT 的机器上
    preflight 报新模型、投影写旧模型，而运行时按后者跑。旋钮在检查器那侧有效、在运行时
    那侧无效，是最难发现的一种。
    """
    import providers

    # 期望值从配置推导，不写死：写死的话它会随配置漂，而漂了之后这条测的就不是覆盖了。
    wanted = providers.as_profiles(CONFIG)["claude-opus"]["model"]
    monkeypatch.setenv("AR_MODEL_AGENT", "claude-opus")

    plan = render_env.detect_conflicts(render_env.claims(CONFIG))

    assert plan["ANTHROPIC_MODEL"]["value"] == wanted


def test_without_an_override_the_config_default_stands(monkeypatch):
    import providers

    monkeypatch.delenv("AR_MODEL_AGENT", raising=False)
    first = providers.role_candidates(CONFIG, "agent")[0]
    wanted = providers.as_profiles(CONFIG)[first]["model"]

    plan = render_env.detect_conflicts(render_env.claims(CONFIG))

    assert plan["ANTHROPIC_MODEL"]["value"] == wanted


def test_an_override_that_names_nothing_is_refused(monkeypatch):
    """静默丢键的后果是消费者退回自己的默认值，而操作者以为覆盖生效了。

    preflight 对同样的输入是响亮报错的，两边要一致。
    """
    monkeypatch.setenv("AR_MODEL_AGENT", "没有这个模型")

    with pytest.raises(render_env.Conflict) as exc:
        render_env.claims(CONFIG)

    assert "AR_MODEL_AGENT" in str(exc.value) and "找不到" in str(exc.value)


def test_a_config_default_that_does_not_resolve_is_not_the_operator_s_fault(monkeypatch):
    """没设环境变量时，配置自己的候选解析不出来只是跳过——那是配置的问题，
    由 preflight 报，不该让投影在这里炸。"""
    import copy

    forked = copy.deepcopy(CONFIG)
    forked["roles"]["agent"]["models"] = ["配置里写错的名字"]

    plan = render_env.detect_conflicts(render_env.claims(forked))

    assert "ANTHROPIC_MODEL" not in plan


# ---- tracked 模板必须与环境无关 ----

@pytest.mark.parametrize("override", ["claude-opus", "没有这个模型"])
def test_the_tracked_template_ignores_a_role_override(override, tmp_path):
    """模板发给所有人，同一份配置必须在任何机器上算出同一份，CI 才 diff 得动。

    `use_env=False` 只跳过 usable_candidates 还不够：`models_for` 自己会读
    `AR_MODEL_<ROLE>`，于是设了覆盖的机器生成出来的模板与别人不同。
    """
    import os
    import subprocess
    import sys

    base = {"PATH": os.environ["PATH"]}
    if home := os.environ.get("HOME") or os.environ.get("USERPROFILE"):
        base["HOME"] = home
    script = str(REPO / "scripts" / "render_env.py")

    clean = subprocess.run([sys.executable, script, "--check"],
                           capture_output=True, text=True, env=base, timeout=120)
    with_override = subprocess.run([sys.executable, script, "--check"], capture_output=True,
                                   text=True, env={**base, "AR_MODEL_AGENT": override},
                                   timeout=120)

    assert clean.returncode == 0, clean.stdout[-400:]
    # 模板本身不该因为覆盖而变化。名字写错时投影仍会拒（那是本机那份的事），
    # 所以判据是「模板没有不同步」，不是整个退出码相同。
    assert "settings.local.example.json 与配置不一致" not in with_override.stdout


def test_reviewer_and_critic_changes_do_not_change_the_projection():
    """MCP roles use the Python bridge, so their routes never become Claude settings."""
    import copy

    forked = copy.deepcopy(CONFIG)
    forked["roles"]["code_reviewer"]["models"] = ["gemini-3.5-flash", "oai-mcp-reviewer"]
    forked["roles"]["critic"]["models"] = ["oai-mcp-reviewer", "gemini-3.5-flash"]

    assert render_env.claims(forked) == render_env.claims(CONFIG)
