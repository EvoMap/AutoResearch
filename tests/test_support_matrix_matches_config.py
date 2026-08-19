"""Public docs describe provider-neutral role routing, not a fixed vendor matrix."""

import json

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
README_CN = REPO / "README_CN.md"


def test_readme_describes_the_two_file_contract():
    contracts = {
        README: "does not require a fixed combination of Gemini, GPT, or Claude",
        README_CN: "不要求固定的 Gemini、GPT 或 Claude 组合",
    }

    for path, provider_claim in contracts.items():
        body = path.read_text(encoding="utf-8")
        assert "config/providers.local.json" in body
        assert "`.env`" in body
        assert provider_claim in body


def test_readme_says_all_consumers_share_roles():
    contracts = {
        README: "The Python pipeline, preflight checks, reviewer MCP, and critic MCP all read this file",
        README_CN: "Python 管线、preflight、reviewer MCP 和 critic MCP 都读取这同一份配置",
    }

    for path, claim in contracts.items():
        assert claim in path.read_text(encoding="utf-8")


def test_readme_requires_panel_diversity_without_requiring_multiple_endpoints():
    contracts = {
        README: (
            "distinct models may share an endpoint",
            "at least three distinct models",
            "2 distinct models",
        ),
        README_CN: ("不同模型可以共用同一个 API endpoint", "至少 3 个不同模型", "2 个不同模型"),
    }

    for path, claims in contracts.items():
        body = path.read_text(encoding="utf-8")
        assert all(claim in body for claim in claims)


def test_readme_groups_every_role_in_a_stable_user_facing_table():
    contracts = (
        (
            README,
            "### 3.2 Configure Model Services",
            ("Stage", "Role", "Purpose", "Model requirement"),
            ("░ Idea signal filtering", "▒ Idea generation and validation", "▓ Idea execution"),
            "Optional; one model when enabled",
        ),
        (
            README_CN,
            "### 3.2 配置模型服务",
            ("阶段", "角色", "作用", "模型要求"),
            ("░ Idea 信号筛选", "▒ Idea 生成与验证", "▓ Idea 执行"),
            "可选；启用时单模型",
        ),
    )

    for path, start_heading, headings, stages, optional_claim in contracts:
        body = path.read_text(encoding="utf-8")
        start = body.index("<table>", body.index(start_heading))
        end = body.index("</table>", start) + len("</table>")
        table = body[start:end]

        for heading in headings:
            assert f"<th>{heading}</th>" in table
        for stage in stages:
            assert stage in table
        for role in (
            "screener",
            "judge",
            "consensus_checker",
            "ideator",
            "planner",
            "freshness_refresher",
            "agent",
            "code_reviewer",
            "critic",
            "critic_secondary",
            "run_monitor",
        ):
            assert f"<code>{role}</code>" in table
        assert "fallback" not in table.lower()
        assert "回退" not in table
        assert optional_claim in table


def test_readme_intro_explains_hallucination_controls_without_overpromising():
    contracts = {
        README: (
            "reduce unsupported generation",
            "real signals",
            "local knowledge base",
            "cross-model review",
            "source records",
            "cannot guarantee that every conclusion is correct",
        ),
        README_CN: (
            "有效降低",
            "幻觉风险",
            "真实信号",
            "本地知识库",
            "模型交叉评审",
            "来源记录",
            "不能保证结论一定正确",
        ),
    }

    for path, claims in contracts.items():
        body = path.read_text(encoding="utf-8")
        intro = body[: body.index("## 3.")]
        assert all(claim in intro for claim in claims)


def test_readme_intro_leads_with_the_product_promise_without_claiming_paper_output():
    for path in (README, README_CN):
        body = path.read_text(encoding="utf-8")
        intro = body[: body.index("## 3.")]

        assert "From Idea to Paper-Ready Evidence" in intro
        assert "Insight In, Hallucination Out." in intro
        assert "Get a Paper" not in intro
        assert "Fully autonomous" not in intro


def test_readme_intro_uses_the_workflow_diagram():
    contracts = (
        (README, "docs/diagrams/autoresearch-workflow.svg", "Sources:"),
        (README_CN, "docs/diagrams/autoresearch-workflow-cn.svg", "依据"),
    )

    for path, diagram, source_marker in contracts:
        body = path.read_text(encoding="utf-8")
        intro = body[: body.index("## 3.")]

        assert f"]({diagram})" in intro
        assert (REPO / diagram).is_file()
        svg = (REPO / diagram).read_text(encoding="utf-8")
        assert "#47C9E7" in svg
        assert source_marker in svg
        assert "论文 / 社区 / 媒体 / 开源趋势（A） ─┐" not in intro


def test_readme_multi_model_example_is_valid_and_focused_on_roles():
    required_roles = {
        "screener", "judge", "consensus_checker", "ideator", "planner",
        "freshness_refresher", "agent", "code_reviewer", "critic",
    }
    contracts = {
        README: "The following fragment shows role mapping only",
        README_CN: "下面只展示角色映射的写法",
    }

    for path, marker in contracts.items():
        body = path.read_text(encoding="utf-8")
        start = body.index("```json", body.index(marker)) + len("```json")
        end = body.index("```", start)
        example = json.loads(body[start:end])

        assert set(example) == {"request_defaults", "roles"}
        assert example["request_defaults"]["max_tokens"] >= 8192
        assert required_roles <= set(example["roles"])
        assert set(example["roles"]["ideator"]["models"]) == {
            "claude-opus-4.8",
            "gemini-3.1-pro",
            "gpt-5.5",
        }


def test_provider_guide_defines_supported_dialects():
    body = (REPO / "docs" / "llm_provider_setup.md").read_text(encoding="utf-8")
    for dialect in ("openai_chat", "openai_responses", "anthropic_messages"):
        assert dialect in body


def test_gpt_researcher_is_declared_separate():
    body = (REPO / "docs" / "llm_provider_setup.md").read_text(encoding="utf-8")
    assert "requirements-research.txt" in body
    assert "scripts/research_to_knowledge.py" in body
    assert "official upstream" in body
    assert "providers.local.json" in body and "does not" in body
    assert "EvoMap/gpt-researcher" not in body
