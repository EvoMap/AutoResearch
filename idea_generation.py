"""Idea Generation：联网研究信号（A）+ 本地知识库（B）。

采集并筛选近期信号，结合本地知识生成候选 Idea，再完成多模型交叉评审、
共识检查和实验计划生成。
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent / "src" / "collectors"))

import seed_ranking  # noqa: E402  路径插入之后才可导入
from api_retry import RetryError, retry_call  # noqa: E402
from verdicts import STRONG, WORTH, conclusion_verdict  # noqa: E402


class ScoreBatchError(RuntimeError):
    """一批标题重试后仍无法得到可用分数。"""


def score_titles(batch, attempts=3, retry_delay=2):
    """让模型给一批标题打 1-10 分，返回分数表。

    端点异常、空返回、格式错误和分数数量不匹配都会重试。全部尝试失败时抛出
    带每次具体原因的异常，由上层记录并决定是否回退。
    """
    import re

    from llm_client import call_role

    numbered = "\n".join(f"{i+1}. {it['title']}" for i, it in enumerate(batch))
    prompt = f"对以下研究内容打分1-10（创新价值）：\n{numbered}\n\n只返回JSON数组。"
    def request_and_parse():
        # 外层还要校验返回格式，所以 provider 自身只尝试一次，避免 3×3 嵌套重试。
        reply = call_role(
            "screener", prompt, temperature=0, attempts=1)
        if not reply:
            raise ValueError("模型没有返回内容")

        match = re.search(r"\[[\d\s,\.]+\]", reply)
        if not match:
            raise ValueError(f"返回格式错误，期待数字 JSON 数组：{reply!r}")

        scores = json.loads(match.group())
        if len(scores) != len(batch):
            raise ValueError(
                f"分数数量不一致：应有 {len(batch)} 个，实际 {len(scores)} 个")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               for value in scores):
            raise ValueError("分数数组中存在非数字值")
        if any(value < 1 or value > 10 for value in scores):
            raise ValueError("分数必须介于 1 到 10")
        return scores

    try:
        return retry_call(
            request_and_parse,
            attempts=attempts,
            delay_for=lambda attempt, _result, _error: retry_delay * attempt,
            operation_name="种子标题评分",
        )
    except RetryError as exc:
        raise ScoreBatchError(str(exc)) from exc


LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_DIR / f"daily_{datetime.now().strftime('%Y%m%d')}.log", "a") as f:
        f.write(line + "\n")


def run_dalang_taosha():
    """模块一：大浪淘沙 - 信息采集 + 筛选种子"""
    log("=" * 60)
    log("模块一：大浪淘沙 - 信息采集与热点筛选")
    log("=" * 60)

    from pipeline_v4 import run_pipeline_v4
    result = run_pipeline_v4()
    return result


def run_extra_channels():
    """已合并进 pipeline_v4.collect_all_channels()，此函数保留为空壳以防老代码引用"""
    log("  附加渠道已并入 pipeline_v4，跳过独立采集")
    return []


def run_idea_forge(seeds):
    """模块二：Idea Forge - 领域交叉生成、验证与计划书"""
    log("=" * 60)
    log(f"模块二：Idea Forge - {len(seeds)} 个种子")
    log("=" * 60)

    from idea_forge.forge import run_idea_forge
    from idea_forge.b_library import select_b_directions, print_selection

    # Use every configured domain direction.
    selected, why = select_b_directions()
    print_selection(selected, why)
    result = run_idea_forge(seeds, b_ids=[b["id"] for b in selected])
    return result


def update_dashboard():
    """更新网页"""
    log("更新网页...")
    from generate_dashboard import generate_html
    generate_html()


def git_push():
    """推送到 GitHub Pages（已暂停使用）"""
    # import subprocess
    # try:
    #     subprocess.run(
    #         ["git", "add", "-A"],
    #         cwd=str(Path(__file__).parent), capture_output=True
    #     )
    #     subprocess.run(
    #         ["git", "commit", "-m", f"Daily auto: {datetime.now().strftime('%Y-%m-%d')}"],
    #         cwd=str(Path(__file__).parent), capture_output=True
    #     )
    #     subprocess.run(
    #         ["git", "push", "origin", "gh-pages"],
    #         cwd=str(Path(__file__).parent), capture_output=True
    #     )
    #     log("  Git push 完成")
    # except Exception as e:
    #     log(f"  Git push 失败: {e}")
    log("  Git push 已暂停")


# Ceiling on how many candidates the 3-month run sends to Pro judgment. Every one
# is a paid call, so an unbounded default turns a wide collection window into an
# unbounded bill.
THREE_MONTH_TOP_K = int(os.environ.get("AR_3MONTH_TOP_K", "500"))


def run_3month_mode():
    """3个月全量模式：采集→Pro全量研判→强推荐种子→大规模Forge"""
    log("=" * 60)
    log("【3月全量模式】 12渠道 × 90天 → Pro全量研判 → 强推荐种子 → Forge")
    log("=" * 60)

    from pipeline_v4 import run_pipeline_v4
    from idea_forge.forge import run_idea_forge
    from idea_forge.b_library import select_b_directions, print_selection

    tag = f"3month_{datetime.now().strftime('%Y%m%d_%H%M')}"
    result = run_pipeline_v4(
        time_filter="year", time_filter_days=90, arxiv_days=90, hf_days=90,
        # Keep a real ceiling. The comment here used to say 500 amounted to no
        # limit, which was true only while top_k did nothing; removing the cap
        # entirely would have left cost, wall-clock and rate-limit exposure with
        # no upper bound on a 12-channel 90-day window. 355 is the largest run on
        # record, so 500 has headroom and still bounds a bad day. Raise it with
        # AR_3MONTH_TOP_K when a run genuinely needs more.
        top_k=THREE_MONTH_TOP_K,
        output_tag=tag,
    )

    # 直接从 Pro 研判结果取强推荐
    final_candidates = result.get("final_candidates", [])
    strong_seeds = [c for c in final_candidates if conclusion_verdict(c) == STRONG]
    worth_seeds  = [c for c in final_candidates if conclusion_verdict(c) == WORTH]
    top_seeds = (strong_seeds + worth_seeds)[:50]

    log(f"  Pro研判: {len(final_candidates)} 条 → 强推荐 {len(strong_seeds)} + 值得深入 {len(worth_seeds)}")
    log(f"  送入 Forge 种子: {len(top_seeds)} 条")

    selected, why = select_b_directions()
    print_selection(selected, why)
    b_ids = [b["id"] for b in selected]
    log(f"  开始大规模 Forge: {len(top_seeds)} 个研究信号 × {len(b_ids)} 个领域方向 × ideator 模型面板")
    forge_result = run_idea_forge(top_seeds, b_ids=b_ids)
    s = forge_result.get("summary", {})
    log(f"  Forge完成: ideas={s.get('total_ideas')} validated={s.get('total_validated')} plans={s.get('total_plans')}")
    return forge_result


def main():
    start = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    log(f"{'═' * 60}")
    log(f"每日全流程开始: {today}")
    log(f"{'═' * 60}")

    # 检测3月全量触发文件
    trigger = Path(__file__).parent / "trigger_3month.txt"
    if trigger.exists():
        log("  检测到 trigger_3month.txt，切换到3月全量模式")
        trigger.unlink()  # 删除触发文件，避免重复执行
        try:
            run_3month_mode()
        except Exception as e:
            log(f"  3月全量模式失败: {e}")
        update_dashboard()
        try:
            from generate_idea_page import generate as gen_idea
            gen_idea()
        except Exception as e:
            log(f"Idea页面更新失败: {e}")
        git_push()
        elapsed = time.time() - start
        log(f"全流程完成，耗时 {elapsed/60:.1f} 分钟")
        return

    # 1. 大浪淘沙
    try:
        v4_result = run_dalang_taosha()
    except Exception as e:
        log(f"大浪淘沙失败: {e}")
        v4_result = None

    # 2. 附加渠道（已并入 pipeline_v4，这里只留日志）
    try:
        run_extra_channels()
    except Exception as e:
        log(f"附加渠道失败: {e}")

    # 3. 提取强推荐种子（优先用 all_insightful 批量评分，fallback 到 final_candidates）
    seeds = []
    if v4_result:
        all_insights = v4_result.get("all_insightful", [])
        if len(all_insights) >= 20:
            # 有足够insight，批量评分选top15
            log(f"  使用 all_insightful 批量评分 ({len(all_insights)} 条)")
            seeds, coverage = seed_ranking.rank(all_insights, score_titles)
            log(f"  批量评分选出 {len(seeds)} 个种子：{coverage.describe()}")
            for failure in coverage.failures:
                log(f"  ⚠ 种子评分失败：{failure}")
            if not coverage.ranked:
                message = "批量评分重试后全部失败，停止流程；请根据上方错误检查模型服务"
                log(f"❌ {message}")
                raise ScoreBatchError(message)
            elif seeds and seed_ranking.SCORE_KEY in seeds[0]:
                lo = seeds[-1].get(seed_ranking.SCORE_KEY, seeds[0][seed_ranking.SCORE_KEY])
                log(f"  分数: {lo:.1f}~{seeds[0][seed_ranking.SCORE_KEY]:.1f}")
        else:
            # insight 数量少，直接用强推荐
            candidates = v4_result.get("final_candidates", [])
            seeds = [c for c in candidates if conclusion_verdict(c) == STRONG]
    log(f"进入 Forge 的种子: {len(seeds)} 个")

    # 如果今天没有新种子，翻历史文件找（从最近往前）
    if not seeds:
        import glob
        verified_dir = Path(__file__).parent / "data" / "verified"
        all_files = sorted(
            glob.glob(str(verified_dir / "final_*.json")) +
            glob.glob(str(verified_dir / "pipeline_v4_*.json")) +
            glob.glob(str(verified_dir / "pipeline_v5_*.json")),
            reverse=True
        )
        all_seeds = {}  # title -> seed dict
        for fpath in all_files[:10]:
            try:
                with open(fpath) as f:
                    fdata = json.load(f)
                cands = fdata.get("final_candidates", fdata.get("top_candidates", []))
                for c in cands:
                    if conclusion_verdict(c) == STRONG:
                        title = c.get("title", "")
                        if title and title not in all_seeds:
                            all_seeds[title] = c
            except Exception:
                continue
        seeds = list(all_seeds.values())
        log(f"从历史文件累积强推荐种子: {len(seeds)} 个")

    # 4. Run domain-intersection ideation for every strong signal.
    if seeds:
        try:
            forge_result = run_idea_forge(seeds)
            log(f"Forge 结果: {forge_result['summary']}")
        except Exception as e:
            log(f"Idea Forge 失败: {e}")
    else:
        log("无种子可处理，跳过 Forge")

    # 5. 更新网页（主页 + Idea Forge 页）
    try:
        update_dashboard()
    except Exception as e:
        log(f"主页更新失败: {e}")

    try:
        from generate_idea_page import generate as gen_idea
        gen_idea()
        log("Idea Forge 页面已更新")
    except Exception as e:
        log(f"Idea 页面更新失败: {e}")

    # 6. 推送
    git_push()

    elapsed = time.time() - start
    log(f"{'═' * 60}")
    log(f"全流程完成，耗时 {elapsed/60:.1f} 分钟")
    log(f"{'═' * 60}")


if __name__ == "__main__":
    main()
