"""面板角色的模型覆盖要落到不同席位上，而不是所有席位探同一个模型。

Idea Forge 要求至少三个独立模型。`AR_MODEL_IDEATOR` 接受多个名字正是为此。但
`model_for_role` 对面板角色也只返回 `[0]`，`evaluate` 把它塞进
每个候选，于是 `a,b,c` 五个席位全探 `a`；而 `model_identity` 取的是覆盖**前**的 profile，
所以「not counted twice」这道去重从不触发，报告照旧宣称五个不同模型。绿的是假的。

覆盖给的是「跑这几个模型」，候选列表给的是「它们能发到哪些端点」。所以名字按顺序落到
可用的候选上，而不是按下标硬绑——硬绑的话，第一个候选缺凭证就会把一个本来能跑的模型
一起废掉。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "providers.example.json"

# 让 ideator 的 oai-* 三个候选可用，其余候选因缺凭证而 skip。
ONE_ENDPOINT = {
    "OPENAI_BASE_URL": "https://example.invalid/v1",
    "OPENAI_API_KEY": "test-key",
}
OTHER_PROVIDERS = ("GEMINI_", "ANTHROPIC_", "AZURE_", "GATEWAY_", "EVOMAP_",
                   "GOOGLE_", "GPT_CRITIC_", "AR_MODEL_")

RUNTIME_CONFIG = {
    "version": 2,
    "endpoints": {
        "test": {
            "dialect": "openai_chat",
            "base_url_env": "OPENAI_BASE_URL",
            "credential_env": ["OPENAI_API_KEY"],
        },
    },
    "models": {
        name: {"routes": [{"endpoint": "test", "wire_name": name}]}
        for name in ("a", "b", "c")
    },
    "roles": {
        "ideator": {
            "models": ["a", "b", "c"],
            "_all_candidates_used": True,
            "_min_available": 3,
        },
    },
}


@pytest.fixture(scope="module")
def runtime_config(tmp_path_factory):
    path = tmp_path_factory.mktemp("cfg") / "providers.local.json"
    path.write_text(json.dumps(RUNTIME_CONFIG), encoding="utf-8")
    return path


def run_ideator(override: str | None, runtime_config: Path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(OTHER_PROVIDERS)}
    env.update(ONE_ENDPOINT)
    env["AUTORESEARCH_CONFIG"] = str(runtime_config)
    if override is not None:
        env["AR_MODEL_IDEATOR"] = override
    done = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "preflight.py"),
         "--config", str(runtime_config), "--role", "ideator"],
        capture_output=True, text=True, env=env, timeout=180)
    return done.returncode, done.stdout


@pytest.mark.parametrize("override,distinct,expected_code", [
    ("a,a,a", 1, 1),
    ("a,a,b", 2, 1),
    ("a,b,c", 3, 0),
])
def test_the_panel_is_judged_on_distinct_models(override, distinct, expected_code,
                                                runtime_config):
    """同一个模型的多个名字仍只算一个意见，少于三种时硬失败。"""
    code, out = run_ideator(override, runtime_config)

    assert code == expected_code, f"{override} 应有 {distinct} 个独立模型\n{out[-1200:]}"


def test_each_seat_probes_its_own_model(runtime_config):
    """三个名字要出现在三个席位上，不是同一个名字出现三次。"""
    _, out = run_ideator("a,b,c", runtime_config)

    for name in ("a", "b", "c"):
        assert f"（{name}）" in out, f"{name} 没有落到任何席位上\n{out[-1200:]}"


def test_a_collapsed_panel_says_why(runtime_config):
    """报告要说清是「三个名字指向同一个模型」，而不是只丢一个退出码。"""
    _, out = run_ideator("a,a,a", runtime_config)

    assert "not counted twice" in out, out[-1200:]
    assert "only 1 of 3 required models available" in out, out[-1200:]


def test_the_recommendation_line_lists_every_seat(runtime_config):
    """`:1100` 读的是 `min_available` / `use_all_available`，配置拼的是 `_min_available` /
    `_all_candidates_used`，所以面板分支是死的：五个席位的角色只打印一个推荐模型。"""
    _, out = run_ideator(None, runtime_config)

    line = next(ln for ln in out.splitlines() if ln.strip().startswith("推荐"))
    assert line.count(",") >= 1, f"面板角色的推荐应该是一组模型，实际是：{line}"


def test_a_model_the_runtime_cannot_call_is_not_reported_as_usable(runtime_config):
    """同类反向：覆盖名不校验能否解析，preflight 就会给一个运行时会抛 UnknownModel
    的名字开绿灯——检查器绿、跑起来这一步直接异常。"""
    code, out = run_ideator("definitely-not-a-configured-model,b,c", runtime_config)

    assert code != 0
    assert "definitely-not-a-configured-model" in out
    assert "UnknownModel" in out or "运行时调不了" in out, out[-1200:]
