"""把模型的话变成一个判定，只在一个地方做。

原先四个文件各写一遍子串测试，两个方向都出过错：

  「Verdict: PASS - a novel mechanism」被判成不通过，因为否定词表里有 "no"，
  而它出现在 novel 里；「通过，不过实验略少」同理，"不过" 是转折词。8 例测 4 例错。

  「不值得深入」被判成「值得」，因为下游测的是 `"值得" in conclusion`。

反过来，历史语料里有 8 条判定确实是【值得深入】而「不适合」出现在理由里
（「但不适合作为即插即用的工程提点模块」）。所以不能反过来让否定词优先：判定要先定位到
判定段落，再在段落里匹配受控词表。这两类反例一起构成这个文件的边界。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from verdicts import (  # noqa: E402
    FAIL,
    PASS,
    SKIP,
    STRONG,
    UNPARSED,
    WORTH,
    conclusion_verdict,
    parse_judge_conclusion,
    parse_reviewer_verdict,
    reviewer_verdict_line,
)


# ---- 评审判定：通过 / 不通过 / 读不出 ----

@pytest.mark.parametrize(
    "line, want",
    [
        ("最终判定: 通过", PASS),
        ("最终判定: 不通过", FAIL),
        ("verdict: 通过", PASS),
        ("verdict: 不通过", FAIL),
        ("Verdict: PASS", PASS),
        ("Verdict: accept", PASS),
        ("Verdict: reject", FAIL),
        ("Verdict: FAIL", FAIL),
        ("**最终判定：通过**", PASS),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_clean_verdict_line_reads_as_itself(line, want):
    assert parse_reviewer_verdict(f"D1 通过\nD2 通过\n{line}") == want


@pytest.mark.parametrize(
    "line",
    [
        "Verdict: PASS - a novel mechanism",
        "Verdict: PASS, notable contribution",
        "最终判定: 通过，不过实验略少",
        "最终判定: 通过。虽然实验不够充分",
    ],
)
def test_a_reason_after_the_verdict_does_not_flip_it(line):
    """理由里的 novel / notable / 不过 曾经把通过判成不通过。"""
    assert parse_reviewer_verdict(line) == PASS


@pytest.mark.parametrize(
    "line",
    [
        "最终判定: 不通过，尽管机制新颖",
        "Verdict: reject - novel but not sound",
    ],
)
def test_a_reason_does_not_rescue_a_rejection(line):
    """反方向的负控：别为了修误判把明确的否决也放过去。"""
    assert parse_reviewer_verdict(line) == FAIL


@pytest.mark.parametrize(
    "text",
    [
        "",
        "这个想法很有意思，我需要更多信息。",
        "D1 通过\nD2 不通过\nD3 通过",  # 逐项有判定，但没有最终判定行
        "最终判定: 待定",
        "最终判定:",
    ],
)
def test_no_verdict_line_is_not_a_verdict(text):
    """读不出是第三种状态。当成不通过会误杀，当成通过会放行。"""
    assert parse_reviewer_verdict(text) == UNPARSED


def test_the_final_verdict_wins_over_the_per_dimension_ones():
    """评审会在逐项里写 D1 判定 / D2 判定，最终判定在最后一行。"""
    text = "D1 判定: 通过\nD2 判定: 不通过\nD3 判定: 通过\n最终判定: 通过"
    assert parse_reviewer_verdict(text) == PASS


def test_a_truncated_review_is_unparsed_not_its_last_dimension():
    """真实数据里的形状：`[D1] 机制深度` 下面跟着 `**判定：不通过**`。

    响应被 max_tokens 截断时，最后一个判定行就是某一维的判定。把它当成最终判定，
    等于让 D1 一票决定整条 idea，而 D1 不通过在完整评审里未必导致否决。
    """
    text = "[D1] 机制深度\n\n**判定：不通过**\n\n**理由：** 映射停留在表面类比层面，缺乏数学形式化"
    assert parse_reviewer_verdict(text) == UNPARSED


# ---- judge 判定：强推荐 / 值得深入 / 不适合 / 读不出 ----

# ai-tell-scan: ignore  下面是判定原文，破折号是语料本身带的
@pytest.mark.parametrize(
    "text, want",
    [
        ("最终判定: 强推荐 —— 机制清楚", STRONG),
        ("最终判定: 值得深入 —— 理论价值高", WORTH),
        ("最终判定: 不适合做A种子 —— 是观点文章", SKIP),
        ("【强推荐做A种子】（提供了极佳的工程基建）", STRONG),
        ("**【值得深入】** —— 值得跟踪", WORTH),
        ("**研判结论:** \n【强推荐做A种子】", STRONG),
        ("**研判结论:** \n【值得深入了解】", WORTH),
    ],
)
def test_the_judge_verdict_reads_as_itself(text, want):
    assert parse_judge_conclusion(text).verdict == want


@pytest.mark.parametrize(
    "text",
    [
        "**【值得深入】——它提供了极佳的批判性理论视角，但不适合作为即插即用的工程提点模块（A种子）。",
        "【值得深入】—— 理论价值极高，但不适合硬刚底层预训练。",
        "**【值得深入】（作为长期跟踪方向），但不适合做短期领域交叉种子。",
    ],
)
def test_a_negation_inside_the_reason_does_not_flip_the_verdict(text):
    """历史语料里 8 条长这样。让否定词优先会把它们全判反。"""
    assert parse_judge_conclusion(text).verdict == WORTH


@pytest.mark.parametrize(
    "text, want",
    [
        ("最终判定: 不值得深入", SKIP),
        ("最终判定: 不太值得", SKIP),
        ("最终判定: 谈不上值得", SKIP),
    ],
)
def test_a_negated_verdict_is_not_the_positive_one(text, want):
    """`"值得" in conclusion` 把这三种都判成了值得。"""
    assert parse_judge_conclusion(text).verdict == want


def test_two_verdicts_in_one_line_is_not_a_verdict():
    """判定是有条件的双结论时，取到哪个只取决于关键词顺序，那不是解析。"""
    text = "**【不适合做A种子】（针对纯算法团队） / 【强推荐】（针对SysML/大厂推理团队）**"
    parsed = parse_judge_conclusion(text)
    assert parsed.verdict == UNPARSED
    assert "多个判定" in parsed.reason


# ai-tell-scan: ignore  语料原文
@pytest.mark.parametrize(
    "text",
    ["", "**", "调用失败", "理论框架极其优雅，但工程门槛极高。"],
)
def test_a_judgment_without_a_verdict_is_unparsed(text):
    parsed = parse_judge_conclusion(text)
    assert parsed.verdict == UNPARSED
    assert parsed.line == ""


def test_the_parsed_line_is_kept_for_a_human_to_read():
    """产出里要留下判定那一行，出问题时不用回去翻原始 judgment。"""
    parsed = parse_judge_conclusion("核心insight: ...\n最终判定: 强推荐 —— 机制清楚")
    assert parsed.line == "最终判定: 强推荐 —— 机制清楚"


# ---- 消费者入口：新数据读字段，老数据回退解析 ----

def test_a_stored_verdict_is_used_as_is():
    candidate = {"conclusion": "随便什么", "conclusion_verdict": STRONG}
    assert conclusion_verdict(candidate) == STRONG


def test_an_old_record_without_the_field_is_parsed():
    """data/ 里已有几百条只有 conclusion 字符串的记录，仍要能渲染。"""
    assert conclusion_verdict({"conclusion": "最终判定: 值得深入 —— 理由"}) == WORTH


def test_a_record_with_neither_is_unparsed():
    assert conclusion_verdict({}) == UNPARSED


def test_a_stored_verdict_outside_the_vocabulary_is_unparsed():
    """字段是写进 JSON 的，下一个写它的人未必知道词表。"""
    assert conclusion_verdict({"conclusion_verdict": "maybe"}) == UNPARSED


# ---- 存下来的 conclusion 单行：消费者读的是它，不是完整 judgment ----

# ai-tell-scan: ignore  以下都是 data/ 里的原文
@pytest.mark.parametrize(
    "line, want",
    [
        ("**值得深入**", WORTH),
        ("**值得深入** —— 该工作试图回答一个具有广泛实践意义的基础问题", WORTH),
        ("**强推荐做A种子** | 机制清楚", STRONG),
        ("值得深入 —— 用极简的反例澄清了一个理论边界", WORTH),
    ],
)
def test_a_bold_verdict_line_is_still_a_verdict(line, want):
    """`data/` 里 24 条 conclusion 长这样：加粗判定，没有【】也没有小标题。

    只认小标题和【】会让它们全变成读不出，而旧代码是认的——修一个方向不能换来另一个
    方向的回归。
    """
    assert conclusion_verdict({"conclusion": line}) == want


def test_a_conditional_aside_in_parentheses_is_not_a_second_verdict():
    """真实原文：判定是【值得深入了解】，括号里是「如果你做 X 方向可以视为强推荐」。

    括号里的是限定语不是并列判定。当成双判定会把一个可用的判定丢掉。
    """
    line = ("** 【值得深入了解】（注：若您的研究方向侧重于非自回归生成、长文本全局规划或"
            "跨模态连续空间对齐，可将其视为【强推荐做A种子】）。")
    assert conclusion_verdict({"conclusion": line}) == WORTH


def test_two_parallel_verdicts_are_still_ambiguous():
    """并列的两个判定没有主次，取哪个都只是关键词顺序说了算。"""
    line = "**【不适合做A种子】（针对纯算法团队） / 【强推荐】（针对SysML/大厂推理团队）**"
    assert conclusion_verdict({"conclusion": line}) == UNPARSED


def test_a_verdict_word_buried_in_prose_is_not_a_verdict():
    """判定词出现在句子中间，不在判定位置上，那是行文不是判定。"""
    line = "理论框架极其优雅（用群论、图和流形统一了各类神经网络），但不适合做低成本的领域交叉缝合"
    assert conclusion_verdict({"conclusion": line}) == UNPARSED


def test_a_prose_line_in_a_full_judgment_is_not_mistaken_for_the_verdict():
    """放宽单行解析不能让完整 judgment 里的正文段落冒充判定。"""
    text = ("核心insight: 这个方法值得关注，因为它简洁。\n"
            "资源可行性: 完全可行。\n"
            "最终判定: 不适合做A种子 —— 是观点文章")
    assert parse_judge_conclusion(text).verdict == SKIP


def test_the_line_the_parser_used_is_recoverable():
    """产出里要存得下「凭什么判的」，而不只是「判了什么」。

    模型常在判定之后继续写建议，所以评审的头尾截断都不保证包含判定行。
    """
    text = ("D1 机制深度: 不通过\n"
            "**verdict: 不通过**\n"
            "建议：直接对标现有工作，明确提出新的记忆更新规则。\n" * 3)

    assert reviewer_verdict_line(text) == "**verdict: 不通过**"


def test_no_verdict_line_yields_an_empty_string_not_a_guess():
    assert reviewer_verdict_line("写了一堆但没给判定") == ""
