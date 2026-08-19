"""把模型输出的一行话变成一个判定。

管线里有两处要读模型给的判定：Step 2 的评审投票（通过 / 不通过），和大浪淘沙的 Pro
研判（强推荐 / 值得深入 / 不适合）。两处原先各自用子串测试，散在四个文件里，两个方向
都出过错：理由里的 "novel" 让 "no" 命中而把通过判成不通过；「不值得深入」含「值得」而
被下游当成值得。

这里只做一件事：定位到判定所在的那一段，在段里精确匹配受控词表。匹配不上就是匹配不上，
返回 UNPARSED 而不是倒向任何一边。读不出时该拦还是该放是调用方的策略，不是解析的。

纯函数，不读文件不发请求，所以能用真实历史语料整批回归。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 读不出。两套词表共用，因为调用方对它的处理是一样的：既不是通过也不是否决。
UNPARSED = "unparsed"

# 评审判定
PASS = "pass"
FAIL = "fail"

# Pro 研判
STRONG = "strong"
WORTH = "worth"
SKIP = "skip"

JUDGE_VERDICTS = (STRONG, WORTH, SKIP)

# 判定和它后面的理由之间的分隔。判定只在分隔之前的那一段里找，否则理由里的
# 「但不适合作为工程模块」会把【值得深入】判反，历史语料里 8 条长这样。
# ai-tell-scan: ignore  下面是分隔符的字面量，不是行文里的破折号
_REASON_SEPARATOR = re.compile(r"——|--|[,，。;；:：|｜]|\s[-–—]\s|\(|（|\+")

# 自称是最终判定的小标题。提示词要求写「最终判定」和 `verdict`，模型也会写「研判结论」，
# 这些变体都在历史数据里见过，认下来比要求模型一字不差更省事。
_FINAL_HEADING = re.compile(
    r"^\W*(?:最终判定|最终结论|研判结论|综合判定|verdict)[\s*:：]*(.*)$",
    re.IGNORECASE,
)

# 泛用的判定小标题。评审会给每个维度各写一行 `**判定：不通过**`，所以这一类不能当成
# 最终判定用：响应被 max_tokens 截断时，最后一个这样的行就是某一维的判定，拿它当结论
# 等于让 D1 一票定生死。只有 judge 那侧用得上它，它的输出里没有逐项判定。
_ANY_HEADING = re.compile(
    r"^\W*(?:最终判定|最终结论|研判结论|综合判定|判定|结论|verdict)[\s*:：]*(.*)$",
    re.IGNORECASE,
)

# 否定式在前，因为它包含肯定式：「不通过」含「通过」，「不值得」含「值得」。匹配到的部分
# 会从段里消掉再匹配下一条，所以「不值得深入」只会得到一个 SKIP，而不是 SKIP 加 WORTH。
_REVIEWER_RULES = (
    (FAIL, re.compile(r"不通过|未通过|不合格|不予通过|\bfail\b|\breject\b|\bno\b", re.I)),
    (PASS, re.compile(r"通过|合格|\bpass\b|\baccept\b|\byes\b", re.I)),
)

_JUDGE_RULES = (
    (SKIP, re.compile(r"(?:不|未|谈不上|算不上)(?:太|够)?值得|不适合|不推荐|不建议")),
    (STRONG, re.compile(r"强推荐|强烈推荐")),
    (WORTH, re.compile(r"值得")),
)


# 括号里是限定语，不是并列判定。「【值得深入了解】（注：若你做 X 方向可视为【强推荐】）」
# 的判定是值得深入了解，把括号里那个也算上会让一个可用的判定变成「读不出」。
_QUALIFIER = re.compile(r"（[^）]*）|\([^)]*\)")


def _verdict_segment(line: str) -> str:
    """判定所在的那一段：去掉 markdown 标记，截到理由开始之前。

    形如 `**【值得深入】…但不适合…` 的判定段是 `值得深入`。整行匹配会同时看到两个词。
    """
    text = _QUALIFIER.sub("", line.strip().strip("*# 　"))
    bracketed = re.findall(r"[【\[]([^】\]]+)[】\]]", text)
    if bracketed:
        # 去掉限定语之后仍有多个 【判定】，那是真的并列，调用方要能看出来，所以都留下。
        return " / ".join(bracketed)
    return _REASON_SEPARATOR.split(text, maxsplit=1)[0]


def _matches(segment: str, rules) -> list[str]:
    """段里出现了哪些判定，按词表顺序，每个匹配消掉之后再找下一个。

    消掉是必须的：否定式包含肯定式，「不值得深入」在不消的情况下会同时命中 SKIP 和
    WORTH，看起来像一行里给了两个判定。真正的双判定（「不适合…… / 强推荐……」）在消掉
    否定式之后仍然剩下一个肯定式，两者因此区分得开。
    """
    remaining, found = segment, []
    for verdict, pattern in rules:
        remaining, hit = pattern.subn("", remaining)
        if hit:
            found.append(verdict)
    return found


def _match(segment: str, rules) -> str:
    hits = _matches(segment, rules)
    return hits[0] if hits else UNPARSED


def parse_reviewer_verdict(text: str) -> str:
    """Step 2 的评审给的是通过还是不通过，或者读不出。

    提示词要求最后一行写 `verdict: 通过`。只认自称最终判定的那一行：评审给每个维度都写
    一行 `**判定：不通过**`，响应被截断时那就是最后一个判定行，当成结论会让 D1 一票定
    生死。从后往前找，因为完整评审里最终判定在最后。
    """
    for line in reversed((text or "").splitlines()):
        heading = _FINAL_HEADING.match(line.strip())
        if not heading:
            continue
        verdict = _match(_verdict_segment(heading.group(1)), _REVIEWER_RULES)
        if verdict != UNPARSED:
            return verdict
    return UNPARSED


def reviewer_verdict_line(text: str) -> str:
    """解析器实际据以判定的那一行。

    产出里存评审的头 160 字和尾 200 字都不保证包含它：模型会在判定之后继续写建议。
    存下这一行，才能事后确认这一票是照着模型自己写的判定记的。
    """
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        heading = _FINAL_HEADING.match(stripped)
        if heading and _match(_verdict_segment(heading.group(1)), _REVIEWER_RULES) != UNPARSED:
            return stripped
    return ""


@dataclass(frozen=True)
class ParsedConclusion:
    """判定、它来自哪一行、以及读不出时的原因。

    留住原始行是为了排查：产出里能直接看到 judge 写了什么，不用回去翻 llm_judgment。
    """

    verdict: str
    line: str = ""
    reason: str = ""


# 判定段最多这么长。存下来的 conclusion 是「判定 + 理由」，截到理由之前只剩判定本身；
# 一个正文句子的首段通常更长，用它把「判定行」和「提到判定词的正文」分开。
_MAX_VERDICT_SEGMENT = 24


def parse_conclusion_line(line: str) -> ParsedConclusion:
    """已经确定是判定行的一行，宽松地读它。

    产出里的 `conclusion` 就是这样一行，`data/` 里 24 条是 `**值得深入** —— 理由`
    这种加粗写法：没有小标题也没有【】。完整 judgment 不能这么宽松地扫，那会让正文里
    提到判定词的句子冒充判定，所以两个入口分开。
    """
    body = (line or "").strip()
    # 存下来的判定行有时带着小标题（`最终判定: 值得深入 —— 理由`），先把它剥掉，
    # 否则截到冒号就只剩小标题本身。
    heading = _ANY_HEADING.match(body)
    if heading and heading.group(1):
        body = heading.group(1)
    segment = _verdict_segment(body)
    if len(segment) > _MAX_VERDICT_SEGMENT:
        return ParsedConclusion(UNPARSED, "", "判定词不在判定位置上")
    hits = _matches(segment, _JUDGE_RULES)
    if len(hits) > 1:
        return ParsedConclusion(UNPARSED, line, f"一行里有多个判定：{'、'.join(hits)}")
    if hits:
        return ParsedConclusion(hits[0], line)
    return ParsedConclusion(UNPARSED, "", "没有找到判定")


def parse_judge_conclusion(text: str) -> ParsedConclusion:
    """Pro 研判给的是哪一档，或者读不出。

    判定可能在 `最终判定:` 同一行，也可能在下一行（模型常写成
    `**研判结论:**` 换行再给 `【强推荐做A种子】`）。也接受没有小标题、直接以
    `【判定】` 开头的行，历史数据里这是最常见的形状。
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        heading = _ANY_HEADING.match(line)
        candidates = []
        if heading:
            candidates.append((heading.group(1), line))
            following = next((x for x in lines[index + 1:] if x), "")
            candidates.append((following, following))
        elif re.match(r"^\W*[【\[]", line):
            candidates.append((line, line))

        for body, source in candidates:
            segment = _verdict_segment(body)
            hits = _matches(segment, _JUDGE_RULES)
            if len(hits) > 1:
                return ParsedConclusion(
                    UNPARSED, source, f"一行里有多个判定：{'、'.join(hits)}"
                )
            if hits:
                return ParsedConclusion(hits[0], source)
    return ParsedConclusion(UNPARSED, "", "没有找到判定")


def conclusion_verdict(candidate: dict) -> str:
    """一条候选的判定档位，给渲染和筛选用。

    优先读 `conclusion_verdict` 字段；`data/` 下几百条老记录只有 `conclusion` 字符串，
    对它们回退到解析。字段值不在词表里时按读不出处理，因为写它的人未必知道词表。
    """
    stored = (candidate or {}).get("conclusion_verdict")
    if stored in JUDGE_VERDICTS:
        return stored
    if stored is not None:
        return UNPARSED
    return parse_conclusion_line((candidate or {}).get("conclusion") or "").verdict
