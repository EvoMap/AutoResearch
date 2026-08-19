"""「这个候选现在能不能用」两边必须给同一个答案。

preflight 会跳过配不通的候选去试下一个，投影（`scripts/render_env.py`）直接取
`providers.usable_candidates` 的第一个。这个判断原来有两份实现，而 #135 就是两份各说
各话的结果：preflight 报 all required roles resolved，投影却把一条没配的 Gemini route
写给了 Claude Code，MCP 起不来。

判据不是「两边代码看起来一样」，是同一份配置加同一组环境，两边选出的候选逐项相同
（#142）。所以这里不读实现，只喂环境比结果：preflight 走它自己的命令行入口，投影侧调
`usable_candidates`，两边都不知道对方存在。

全部离线：不带 `--live` 的 preflight 只判「配没配」，一个请求都不发。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "config" / "providers.example.json"
sys.path.insert(0, str(REPO / "src"))

import providers  # noqa: E402


def clean_env(**extra: str) -> dict[str, str]:
    """只留被测的那几个变量。

    开发机上继承来的 GEMINI_* / ANTHROPIC_* 会让「缺 key」这类负控恒绿——#134 就是
    这么发生的，那次是 preflight 自己的测试，这次两边都要在同一个受控环境里回答。
    """
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG"}}
    env.update(extra)
    return env


OPENAI = {"OPENAI_BASE_URL": "https://example.invalid/v1", "OPENAI_API_KEY": "sk-test"}
GEMINI = {"GEMINI_BASE_URL": "https://gemini.invalid/v1", "GEMINI_API_KEY": "g-test"}
ANTHROPIC = {"ANTHROPIC_BASE_URL": "https://anth.invalid/v1",
             "ANTHROPIC_API_KEY": "sk-anth"}

# anthropic 端点在 v1 形状里可以只声明 api_key：AnthropicMessages.build 发的是
# `auth_token or api_key`，两个头一起带。所以这是一台真能跑的机器。
ANTHROPIC_WITH_API_KEY = {
    "roles": {"solo": {"candidates": ["anth"]}},
    "profiles": {"anth": {"api": "anthropic_messages", "base_url_env": "A_BASE",
                          "api_key_env": "A_KEY", "model": "claude-x"}},
}

# A role pinned to Anthropic Messages must reject an OpenAI Chat route before any request.
ROLE_PINS_THE_DIALECT = {
    "roles": {"solo": {"_requires_api": ["anthropic_messages"], "candidates": ["oai"]}},
    "profiles": {"oai": {"api": "openai_chat", "base_url_env": "OPENAI_BASE_URL",
                         "api_key_env": "OPENAI_API_KEY", "model": "gpt-x"}},
}

CASES = [
    ("单端点", None, "code_reviewer", OPENAI),
    ("双端点", None, "ideator", {**OPENAI, **GEMINI}),
    ("缺 key", None, "code_reviewer", {"OPENAI_BASE_URL": OPENAI["OPENAI_BASE_URL"]}),
    ("缺 URL", None, "code_reviewer", {"OPENAI_API_KEY": OPENAI["OPENAI_API_KEY"]}),
    ("agent 拒绝 openai 方言", None, "agent", OPENAI),
    ("agent 接受 anthropic 方言", None, "agent", ANTHROPIC),
    ("anthropic 只给了 api_key", ANTHROPIC_WITH_API_KEY, "solo",
     {"A_BASE": "https://anth.invalid", "A_KEY": "sk-anth"}),
    ("角色钉死方言", ROLE_PINS_THE_DIALECT, "solo", OPENAI),
]


def preflight_picks(config_path: Path, role: str, env: dict[str, str],
                    report_path: Path) -> list[str]:
    """preflight 这一侧选出的候选，按它自己写进 JSON 报告的顺序。

    读 `ok` 而不是「非 skip」：fail 也不是选中——未知方言、角色要求的方言不匹配都记在
    fail 上，把它算进来会让这条断言在最该红的时候绿。
    """
    done = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "preflight.py"),
         "--config", str(config_path), "--role", role, "--json", str(report_path)],
        capture_output=True, text=True, env=env, timeout=180)
    assert report_path.exists(), (
        f"preflight 没写出报告（exit {done.returncode}）：\n{done.stdout[-800:]}\n{done.stderr[-800:]}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return [name for name, result in report["roles"].get(role, {}).items()
            if result["status"] == "ok"]


@pytest.mark.parametrize("name, config, role, extra", CASES, ids=[c[0] for c in CASES])
def test_both_sides_pick_the_same_candidates(name, config, role, extra, tmp_path) -> None:
    path = EXAMPLE
    if config is not None:
        path = tmp_path / "providers.json"
        path.write_text(json.dumps(config), encoding="utf-8")
    env = clean_env(**extra)

    # 传同一份配置和同一组环境。`--config x` 就是 x，preflight 不会再合并别的文件，
    # 所以两边读的确实是同一棵树。
    loaded = json.loads(path.read_text(encoding="utf-8"))
    usable = providers.usable_candidates(loaded, role, env)
    role_spec = loaded["roles"][role]
    projection = usable if role_spec.get("_all_candidates_used") else usable[:1]
    checker = preflight_picks(path, role, env, tmp_path / "report.json")

    assert projection == checker, (
        f"{name}：投影选 {projection}，preflight 选 {checker}。"
        f"两侧对「能不能用」的判断已经分叉")
