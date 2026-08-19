"""Provider onboarding must stop at the protocol boundary each consumer implements."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "providers.example.json"
README = REPO / "README.md"
README_CN = REPO / "README_CN.md"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import providers  # noqa: E402

OPENAI_ENDPOINT = {
    "OPENAI_BASE_URL": "https://example.invalid/v1",
    "OPENAI_API_KEY": "test-key",
}
ANTHROPIC_ENDPOINT = {
    "ANTHROPIC_BASE_URL": "https://anthropic.example.invalid/v1",
    "ANTHROPIC_API_KEY": "anthropic-test-key",
}
# 会让别的候选先命中的变量，测最小配置时要摘掉。
OTHER_PROVIDERS = (
    "GEMINI_", "ANTHROPIC_", "AZURE_", "GATEWAY_", "EVOMAP_", "GOOGLE_", "GPT_CRITIC_",
)


def run_preflight(env_extra, args=()):
    env = {k: v for k, v in os.environ.items() if not k.startswith(OTHER_PROVIDERS)}
    # These are test inputs. Inheriting workstation credentials made negative controls pass (#134).
    for name in (*OPENAI_ENDPOINT, *ANTHROPIC_ENDPOINT, "CLAUDE_CODE_USE_OPENAI"):
        env.pop(name, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "preflight.py"),
         "--config", str(CONFIG), *args],
        capture_output=True, text=True, env=env, timeout=180)


def test_openai_and_anthropic_protocol_endpoints_cover_every_required_role():
    """Python roles speak OpenAI Chat while the official CLI speaks Anthropic Messages."""
    done = run_preflight({**OPENAI_ENDPOINT, **ANTHROPIC_ENDPOINT})

    assert "all required roles resolved" in done.stdout, done.stdout[-1500:]
    assert done.returncode == 0


def test_readme_puts_live_api_verification_immediately_after_bringup():
    """新用户不应在后文拼出「填凭证后还要实测」这条关键路径。"""
    contracts = {
        README: (
            "## 4. Idea Generation: Cross-Domain Discovery",
            "does not contact model services",
        ),
        README_CN: ("## 4. Idea Generation：领域交叉", "不会向模型服务发送请求"),
    }

    for path, (idea_heading, no_request_claim) in contracts.items():
        body = path.read_text(encoding="utf-8")
        bringup = body.index("bash scripts/bringup.sh")
        env_setup = body.index("test -f .env || cp .env.example .env", bringup)
        live_check = body.index(".venv/bin/python scripts/preflight.py --live", env_setup)
        idea_section = body.index(idea_heading)
        generation_command = body.index(
            ".venv/bin/python idea_generation.py", idea_section
        )

        assert bringup < env_setup < live_check < idea_section < generation_command
        assert no_request_claim in body[bringup:live_check]


def test_readme_explains_the_two_idea_entry_points():
    contracts = {
        README: (
            "Intersect External Research Signals with Local Domain Knowledge",
            "`idea_generation.py` is the recommended entrypoint",
            "`run_pending_forge.py` resumes seeds",
        ),
        README_CN: (
            "外部研究信号如何与本地领域知识交叉",
            "`idea_generation.py`：推荐入口",
            "`run_pending_forge.py`：补跑入口",
        ),
    }

    for path, expected in contracts.items():
        body = path.read_text(encoding="utf-8")
        assert "A" + "+" + "B" not in body
        assert all(phrase in body for phrase in expected)


def test_public_copy_describes_domain_intersection_without_a_b_shorthand():
    public_copy = (
        README,
        README_CN,
        REPO / "idea_generation.py",
        REPO / "src" / "generate_dashboard.py",
        REPO / "src" / "generate_idea_page.py",
        REPO / "src" / "pipeline_v4.py",
        REPO / "src" / "idea_forge" / "b_library.py",
        REPO / "src" / "idea_forge" / "consensus_check.py",
        REPO / "src" / "idea_forge" / "forge.py",
        REPO / "src" / "idea_forge" / "freshness.py",
    )

    for path in public_copy:
        body = path.read_text(encoding="utf-8")
        assert "A" + "+" + "B" not in body, f"{path.relative_to(REPO)} still exposes the legacy shorthand"

    for path in (README, README_CN):
        readme = path.read_text(encoding="utf-8")
        for shorthand in (
            "**A：",
            "**B：",
            "运行 A",
            "运行 B",
            "路径 A",
            "路径 B",
            "交给 B",
        ):
            assert shorthand not in readme, f"{path.name} still exposes {shorthand!r}"


def test_idea_generation_is_the_only_full_pipeline_entrypoint():
    assert (REPO / "idea_generation.py").is_file()
    assert not (REPO / "daily_idea_radar.py").exists()
    assert not (REPO / "daily_full.py").exists()
    assert "idea_generation.py" in (REPO / "run_daily.sh").read_text(encoding="utf-8")


def test_readme_warns_about_restricted_networks():
    contracts = {
        README: ("including mainland China", "may require a proxy", "skips that channel"),
        README_CN: ("中国大陆等网络环境", "可能需要代理", "跳过该渠道并继续"),
    }

    for path, expected in contracts.items():
        body = path.read_text(encoding="utf-8")
        assert all(phrase in body for phrase in expected)


def test_a_retired_flag_cannot_make_openai_chat_usable_by_the_official_cli():
    """A writable environment flag cannot grant a protocol the runtime does not implement."""
    done = run_preflight({**OPENAI_ENDPOINT, "CLAUDE_CODE_USE_OPENAI": "1"})

    assert done.returncode != 0, done.stdout[-1500:]
    assert "required role(s) unresolved: agent" in done.stdout


def test_the_ideator_panel_is_satisfied_by_distinct_models_not_distinct_vendors():
    """判重看的是 model 字符串，所以同一端点上三个模型名就够。

    要是按厂商判重，一个端点永远凑不齐这个席位，上手就必然要两家。
    """
    done = run_preflight(OPENAI_ENDPOINT, args=("--role", "ideator"))

    assert "all required roles resolved" in done.stdout, done.stdout[-800:]


def test_collapsing_the_panel_onto_one_model_is_reported():
    """反方向的负控：把几个 profile 指向同一个模型，不能悄悄算成几票。

    三个席位都解析成同一模型时，静态自检必须阻止 Idea Forge 启动。
    """
    done = run_preflight(
        {**OPENAI_ENDPOINT, "OPENAI_SMALL_MODEL": "same", "OPENAI_MID_MODEL": "same",
         "OPENAI_LARGE_MODEL": "same"},
        args=("--role", "ideator"))

    assert "not counted twice" in done.stdout, done.stdout[-600:]
    assert "of 3 required models available" in done.stdout


@pytest.mark.parametrize("role", ["agent", "code_reviewer", "critic"])
def test_the_roles_that_had_no_fallback_now_have_one(role):
    """这三个角色原来各只有一个候选，任何一家 provider 不可用就整条流程停。"""
    config = json.loads(CONFIG.read_text(encoding="utf-8"))

    assert len(providers.role_candidates(config, role)) >= 2, f"{role} 仍然只有一个候选"


def test_a_profile_can_declare_the_env_its_consumer_needs():
    """`requires_env` belongs to one consumer profile rather than global runtime state."""
    profile = {"api": "custom", "requires_env": {"CONSUMER_MODE": "native"}}

    missing = providers.missing_required_env(profile, env={})
    assert missing == ["CONSUMER_MODE=native"]

    assert providers.missing_required_env(
        profile, env={"CONSUMER_MODE": "native"}) == []
    # A different value does not place the consumer in the profile's declared mode.
    assert providers.missing_required_env(
        profile, env={"CONSUMER_MODE": "legacy"}) == ["CONSUMER_MODE=native"]


# ---- 单端点：preflight 选的那条 route 要一路走到 MCP ----

def clean_env(**extra):
    """只留被测的那几个变量。开发机上继承来的 GEMINI_* / AZURE_* 会让这条测试恒绿。"""
    keep = {"PATH", "HOME", "LANG"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(extra)
    return env


ONE_ENDPOINT_ONLY = {"OPENAI_BASE_URL": "https://example.invalid/v1",
                     "OPENAI_API_KEY": "sk-test"}
DOCUMENTED_SETUP = {**ONE_ENDPOINT_ONLY, **ANTHROPIC_ENDPOINT}


def test_mcp_roles_do_not_need_a_second_projection(tmp_path):
    """Reviewer routes stay in unified JSON and are consumed by the role bridge."""
    import json as _json
    import sys as _sys

    _sys.path.insert(0, str(REPO / "src"))
    _sys.path.insert(0, str(REPO / "scripts"))
    import providers
    import render_env

    config = _json.loads(CONFIG.read_text(encoding="utf-8"))
    usable = providers.usable_candidates(config, "code_reviewer", ONE_ENDPOINT_ONLY)
    assert usable, "单端点下 code_reviewer 应当有可用候选"

    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(ONE_ENDPOINT_ONLY)
        plan = render_env.detect_conflicts(render_env.claims(config))
    finally:
        os.environ.clear()
        os.environ.update(saved)

    assert "GEMINI_BASE_URL" not in plan
    bridge = (REPO / "scripts" / "call_role.py").read_text(encoding="utf-8")
    assert "load_effective_config" in bridge


def test_the_mcp_self_test_requires_a_fresh_projection_without_importing_it():
    script = (REPO / "ar-runtime" / "scripts" / "ar-preflight-mcp.sh").read_text(encoding="utf-8")

    assert "local_is_fresh" in script
    assert "settings.local.json" in script
    assert "scripts/hook_python.sh scripts/render_env.py" in script
    assert 'export "$key=$value"' not in script, "MCP 直接读统一配置，不应重新导入旧投影"


@pytest.mark.parametrize("drop", ["OPENAI_BASE_URL", "OPENAI_API_KEY"])
def test_the_negative_control_fails_without_inheriting_the_dev_machine(drop):
    """缺 URL 或 key 时必须失败。

    判据要在一个干净进程里成立：开发机上 GEMINI_* / AZURE_* 都设着，继承进来的话
    这条负控在最需要它的机器上恒绿——#134 就是这么发生的。
    """
    env = clean_env(**{k: v for k, v in ONE_ENDPOINT_ONLY.items() if k != drop})

    done = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "preflight.py"), "--config", str(CONFIG)],
        capture_output=True, text=True, env=env, timeout=180)

    assert done.returncode != 0, f"缺 {drop} 时不该报 resolved：\n{done.stdout[-500:]}"
    assert drop in done.stdout


def test_the_documented_setup_resolves_every_required_role():
    """The documented four-variable setup must pass configuration preflight."""
    done = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "preflight.py"), "--config", str(CONFIG)],
        capture_output=True, text=True, env=clean_env(**DOCUMENTED_SETUP), timeout=180)

    assert "all required roles resolved" in done.stdout, done.stdout[-600:]
    assert done.returncode == 0
