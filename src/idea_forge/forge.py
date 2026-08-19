"""
Idea Forge - 第二模块核心（重构版）
从研究信号与领域知识的交叉中生成 Idea，再经过严格验证并形成计划书。

核心改进:
- 不让模型自由选择领域，而是从知识库中指定方向并提供完整上下文
- idea 生成 prompt 要求深度（必须说清机制同构性）
- 交叉验证保持严格（目标是顶会水平）
- 只有真正好的 idea 才能进入计划书阶段
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import llm_client
from llm_client import UnknownModel, call_model, call_role
import roles as role_policy
from idea_forge.b_library import select_b_directions, format_b_context
from idea_forge.consensus_check import filter_by_consensus
import resource_profile
from plans import FAILURE_MARKER, has_usable_plan
from verdicts import PASS, UNPARSED, parse_reviewer_verdict, reviewer_verdict_line
from idea_forge.freshness import step2_5_freshness_refresh

# 构思席位。每个席位各自独立思考，交叉验证时全员评审（包括生成者自己）。这个环节的
# 独立性是算法合同：至少三个不同模型，三席时两票构成多数。
#
# 席位名单从配置来（`roles.ideator.models`），preflight 探的是同一份。原来这里写死三个
# 名字，配置里那栏运行时一次都读不到：检查器报的是一组模型，跑起来用的是另一组（#190）。
IDEA_MODELS = llm_client.configured_distinct_role_models("ideator")
MIN_IDEA_MODELS = role_policy.minimum_available("ideator")

PLAN_MODELS = llm_client.configured_role_models("planner")
PLAN_MODEL = PLAN_MODELS[0]
IDEATOR_REQUEST = llm_client.configured_request_params("ideator")


def _run_parallel(function, items):
    """Run one independent-call batch using the latest config-owned cap."""
    work = list(items)
    max_concurrency = llm_client.configured_max_concurrency()
    if len(work) <= 1 or max_concurrency == 1:
        return [function(item) for item in work]
    workers = min(max_concurrency, len(work))
    print(f"  并发批次: {len(work)} 个请求，最多 {workers} 个同时执行")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(function, work))


def _seed_key(seed):
    return (seed.get("seed_key") or seed.get("reddit_url") or seed.get("url")
            or seed.get("seed_url") or seed.get("title") or seed.get("seed_title", ""))


def _summary(results):
    return {
        "seeds_processed": len(results),
        "total_ideas": sum(record.get("total_ideas", 0) for record in results),
        "total_validated": sum(record.get("validated", 0) for record in results),
        "total_plans": sum(record.get("plans", 0) for record in results),
    }


def _atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _save_checkpoint(path, run_config, results):
    payload = {
        "generated_at": datetime.now().isoformat(),
        "module": "idea_forge",
        "config": {
            **run_config,
            "max_concurrency": llm_client.configured_max_concurrency(),
        },
        "results": results,
        "summary": _summary(results),
    }
    _atomic_write_json(path, payload)
    return payload


def require_idea_panel() -> None:
    """Refuse to turn self-review into cross-review by changing the denominator."""
    if len(IDEA_MODELS) >= MIN_IDEA_MODELS:
        return
    raise RuntimeError(
        f"Idea Forge 需要至少 {MIN_IDEA_MODELS} 个不同模型；当前只有 "
        f"{len(IDEA_MODELS)} 个：{', '.join(IDEA_MODELS) or '(未配置)'}。"
        "请修改 roles.ideator.models；同一模型的别名或不同 endpoint 只算一个。"
    )

FRESHNESS_REQUIREMENT = """【时新性引导 - 尽量遵守，但不必担心被一票否决】
当前时间是 2026 年 5 月。**优先使用 2025-2026 年最新的模型、数据集、benchmark**。
若不确定最新版本是什么，请尽量用你**已知最近**的型号；后续 Step 2.5 会通过 B 库 + arxiv 自动刷新升级，
所以构思时**优先把机制讲清楚**，模型/数据细节稍旧也没关系。

参考方向（B 库可能持续更新，以你知道的最新为准）：
- 多模态：Qwen2.5-VL / InternVL3 / LLaVA-OneVision / Cambrian-1 / NVILA / Molmo（或更新者）
- LLM 推理：DeepSeek-R1/V3.1 / Qwen3 / Llama-4 / QwQ / o-series（或更新者）
- Agent 记忆：MemGPT-v2 / Letta / Mem0 / LangMem 2025（或更新者）
- Benchmark：MMMU-Pro / MEGA-Bench / LiveBench / SWE-Bench-Verified / ARC-AGI-2（或更新者）
- 避免：LLaVA-1.5/1.6、Qwen2-VL、InternVL2、Llama-2、Vicuna、GPT-3.5 等已被替代的型号作主基线
"""


def call_idea_model(model_name, prompt, **kwargs):
    """Route every idea model through the same resolver.

    This used to send anything named gemini-* to Vertex regardless of what the config
    said, which made the model's name decide its protocol. Where a model is served
    from belongs in the config, not in a prefix check: a Gemini reachable over an
    OpenAI-compatible endpoint was unusable here even when correctly configured.

    一个席位发不出去只损失它自己那一票：打一行原因，返回 None，其余席位照跑。调用点本来
    就把 None 当「这个席位没给出结果」处理，而缺凭据时 `call_model` 抛的是 RuntimeError，
    整轮构思会停在第一个配歪的端点上。名字不认识（`UnknownModel`）仍然抛：那是配置写错，
    换一个席位也救不了，跟 `call_role` 的分法一致。
    """
    try:
        return call_model(model_name, prompt, **kwargs)
    except UnknownModel:
        raise
    except Exception as exc:                      # noqa: BLE001
        print(f"    [{model_name}] 发不出去: {type(exc).__name__}: {exc}")
        return None


def generate_deep_idea_prompt(seed, b_direction):
    """
    构造深度 idea 生成 prompt
    关键变化: 加入领域方向的完整知识文档，让模型具备必要的领域常识
    """
    title = seed.get("title", "")
    judgment = seed.get("llm_judgment", "")
    # 从 judgment 里提取核心 insight 那一行
    core_insight = ""
    for line in judgment.split("\n"):
        line = line.strip()
        if "核心insight" in line or "核心 insight" in line:
            core_insight = line
            break

    b_context = format_b_context(b_direction, include_full_knowledge=True)

    prompt = (
        "你是一位在目标领域深耕多年的顶级研究者，目标是产出能被 ICLR/NeurIPS/ICML 接收的论文。\n"
        "当前是 2026 年 5 月，你必须使用最新的模型与 benchmark，否则论文会被审稿人秒拒。\n\n"
        "【任务】\n"
        "下面给你一个近期研究信号和一个目标领域方向。\n"
        "领域方向附带了**该领域的真实社区共识、路线之争、常见误区**，你必须严格遵守这些常识。\n\n"
        "请思考：研究信号中的核心机制能否真正解决目标领域的本质问题？\n\n"
        "【研究信号 - 核心 Insight】\n"
        "标题: " + title + "\n"
        "洞见: " + core_insight + "\n\n"
        "【目标领域 - 包含社区真实共识】\n" + b_context + "\n\n"
        "【硬件约束】\n"
        + resource_profile.load().describe() + "\n"
        "- 只能用公开数据集\n\n"
        + FRESHNESS_REQUIREMENT + "\n\n"
        "【核心要求 - 极其重要，必须严格遵守】\n\n"
        "1. **必须尊重目标领域的社区共识**：\n"
        "   - 看\"常见错误直觉\"部分！如果你的 idea 撞到了这些错误直觉，直接放弃，输出 NO_MATCH\n"
        "   - 例如：如果目标是视觉 token 管理，不要说\"按时间衰减\"（因为视觉 token 没时间维度）\n"
        "   - 例如：如果目标是 Agent 记忆，不要说\"直接套遗忘曲线\"（社区不认同强行遗忘）\n\n"
        "2. **必须从目标领域的\"可行创新切入点\"出发**：\n"
        "   - 在 MD 的\"值得做的方向\"里找你要做的事\n"
        "   - 如果研究信号中的机制能给这些方向带来新价值，那就是好 idea\n"
        "   - 否则输出 NO_MATCH\n\n"
        "【输出格式】\n"
        "如果有合理映射，请输出：\n\n"
        "核心idea（一句话）: [用研究信号中的机制解决目标领域的某个具体问题]\n\n"
        "机制同构性: [详细解释为什么该机制在目标领域有效，必须有数学/物理直觉]\n\n"
        "方法简述: [核心修改是什么，3-5句]\n\n"
        "关键实验:\n"
        "- 数据集: [必须是领域知识库或 FRESHNESS 中的最新 benchmark]\n"
        "- 基线: [必须是领域知识库中的最新基线]\n"
        "- 评估指标: [具体指标]\n"
        "- 预期提升: [定量预期]\n\n"
        "如果没有合理映射，只输出：NO_MATCH"
    )
    return prompt


def step1_deep_ideation(seed, b_directions):
    """
    Step 1: 对每个研究信号和领域方向组合，让每个构思席位各自深度构思
    """
    require_idea_panel()
    all_ideas = []
    print("────────────────────────────────────────────────────────────")
    # 席位数从名单来。写死「3 模型」的话，配置里改了席位这行就在说谎。
    print(f"  Step 1: 深度构思 ({len(b_directions)} 个领域方向 × {len(IDEA_MODELS)} 模型)")
    print(f"  研究信号: {seed.get('title', '')[:100]}")

    tasks = []
    for b in b_directions:
        print(f"\n  领域方向: {b.get('domain', '')} | {b.get('problem', '')[:50]}")
        prompt = generate_deep_idea_prompt(seed, b)
        for model in IDEA_MODELS:
            print(f"    [{model}] 构思中...")
            tasks.append((b, model, prompt))

    def ideate(task):
        b, model, prompt = task
        result = call_idea_model(
            model, prompt, **{**IDEATOR_REQUEST, "temperature": 0.3})
        return b, model, result

    for b, model, result in _run_parallel(ideate, tasks):
        if not result:
            print(f"    [{model}] 调用失败")
            continue
        if "NO_MATCH" in result:
            print(f"    [{model}] → 无合理映射")
            continue

        core_line = ""
        for line in result.split("\n"):
            line = line.strip()
            if "核心idea" in line:
                core_line = line
                break

        all_ideas.append({
            "seed_title": seed.get("title", ""),
            "seed_url": seed.get("reddit_url") or seed.get("url", ""),
            "b_domain": b.get("domain", ""),
            "b_id": b.get("id", ""),
            "b_problem": b.get("problem", ""),
            "source_model": model,
            "idea_text": result,
            "core_summary": core_line,
        })

    print(f"\n  共产生 {len(all_ideas)} 个候选 idea")
    return all_ideas


def step2_strict_validation(ideas):
    """
    Step 2: 严格交叉验证（目标顶会水平）
    全员评审：所有 IDEA_MODELS（包括生成者自己）都参与打分，避免单一模型主导否决。

    通过/不通过判定**只看 D1/D2/D3（机制深度、方法简洁、实验充分）**，按配置席位数
    取过半数；同时至少要收到三个可解析的独立评审，不能靠减少有效票数降低门槛。
    D4（时新性）仅记录为 freshness_flags，**不再一票否决**——因为旧模型/数据可在
    Step 2.5 通过 B 库 + arxiv 搜索就地刷新升级，不应击杀好计划。
    """
    require_idea_panel()
    validated = []
    print("────────────────────────────────────────────────────────────")
    pass_threshold = len(IDEA_MODELS) // 2 + 1
    print(f"  Step 2: 严格交叉验证 ({len(ideas)} 个候选 × {len(IDEA_MODELS)} 评审员/全员评审)")
    print(f"  通过门槛：D1/D2/D3 ≥{pass_threshold} 评审员通过；D4 仅作软警告供 Step 2.5 刷新")
    tasks = []
    for idx, item in enumerate(ideas):
        idea_text = item.get("idea_text", "")
        review_prompt = (
                "你是一位顶级 AI 会议（NeurIPS/ICLR/ICML/CVPR）的资深审稿人。\n"
                "当前是 2026 年 5 月。请严格按四个维度评审下面的研究方案。\n\n"
                "=== 方案 ===\n" + idea_text + "\n=== 方案结束 ===\n\n"
                "【评审维度 - 必须逐项给出明确判断】\n\n"
                "[D1] 机制深度: 机制映射是否有数学或物理直觉上的深层道理，而非表面类比硬凑？\n"
                "[D2] 方法简洁: 方法是否能用 2-3 句说清楚核心贡献？过度复杂会扣分。\n"
                "[D3] 实验充分: 实验设计能否 convincingly 验证假设？是否包含必要的消融与对照？\n"
                "[D4] 时新性（仅作软警告，不影响通过判定）:\n"
                "     提到的模型/数据/benchmark 是否是 2025-2026 最新的？\n"
                "     若发现过时项（如 LLaVA-1.5/1.6、Qwen2-VL、InternVL2、Llama-2、Vicuna、GPT-3.5），请列出。\n\n"
                "请逐项输出：D1通过/不通过 + 理由（1句）；D2通过/不通过 + 理由；D3通过/不通过 + 理由；"
                "D4时新性问题（如有则列出，无则写\"无\"）。\n"
                "最后一行输出：verdict: 通过 或 verdict: 不通过\n"
                "（verdict 只基于 D1/D2/D3，D4 不计入）"
        )
        for reviewer in IDEA_MODELS:
            tasks.append((idx, reviewer, review_prompt))

    def review(task):
        idx, reviewer, prompt = task
        result = call_idea_model(
            reviewer, prompt, **{**IDEATOR_REQUEST, "temperature": 0.2})
        return idx, reviewer, result

    responses = {idx: [] for idx in range(len(ideas))}
    for idx, reviewer, result in _run_parallel(review, tasks):
        responses[idx].append((reviewer, result))

    for idx, item in enumerate(ideas):
        source = item.get("source_model", "unknown")
        votes_pass = 0
        total_reviews = 0
        unreadable = 0
        reviews = []
        freshness_flags = []

        for reviewer, result in responses[idx]:
            tag = "(self)" if reviewer == source else ""
            if not result:
                print(f"    [{reviewer}{tag}] ⚠️ 调用失败")
                continue

            verdict = parse_reviewer_verdict(result)
            if verdict == UNPARSED:
                # 和调用失败同样处理。倒向通过会放行没人评审过的 idea，倒向不通过会让
                # 一次格式跑偏顶替一张反对票；两种都是凭空造票。
                unreadable += 1
                print(f"    [{reviewer}{tag}] ⚠️ 判定读不出，不计票")
                continue

            total_reviews += 1
            passed = verdict == PASS
            if passed:
                votes_pass += 1

            # 检查 D4 时新性警告
            d4_stale = False
            for line in result.splitlines():
                line = line.strip().lower()
                if "d4" in line and "无" not in line and len(line) > 10:
                    d4_stale = True
                    freshness_flags.append(line)

            reviews.append({
                "reviewer": reviewer,
                "passed": passed,
                "verdict": verdict,
                "review": result,
                # 单独保留解析器实际据以判定的那一行，方便复核解析结果。
                "verdict_line": reviewer_verdict_line(result),
            })

            reason = "✅ 通过" if passed else "❌"
            print(f"    [{reviewer}{tag}] → {reason} | D4: {'有警告' if d4_stale else 'OK'}")

        pass_rate = votes_pass / max(total_reviews, 1)
        panel_complete = total_reviews >= MIN_IDEA_MODELS
        verdict_pass = panel_complete and votes_pass >= pass_threshold

        # 票数不管过不过都记。只记通过的，被否的 idea 事后就没法复核那几票是怎么来的，
        # 而判定解析出过错的地方正是这里。
        item["validation"] = {
            "votes_pass": votes_pass,
            "total_reviews": total_reviews,
            "unreadable_reviews": unreadable,
            "required_reviews": MIN_IDEA_MODELS,
            "panel_complete": panel_complete,
            "pass_rate": pass_rate,
            "reviews": reviews,
        }
        item["freshness_flags"] = freshness_flags

        if verdict_pass:
            validated.append(item)
            unread = f" [{unreadable} 票读不出]" if unreadable else ""
            print(f"  → ✅ 通过 ({votes_pass}/{total_reviews}){unread}"
                  f"{' [时新性有警告]' if freshness_flags else ''}")
        elif not panel_complete:
            print(f"  → ❌ 评审不足 ({total_reviews}/{MIN_IDEA_MODELS} 个独立评审可解析)，不降级放行")
        else:
            unread = f" [{unreadable} 票读不出]" if unreadable else ""
            print(f"  → ❌ 未通过 ({votes_pass}/{total_reviews}){unread}")

    print(f"\n  通过验证: {len(validated)}/{len(ideas)}")
    return validated


def step3_plan_generation(validated_ideas):
    """
    Step 3: 为通过验证的 idea 生成详细计划书（预实验 + 完整计划）
    """
    print("────────────────────────────────────────────────────────────")
    print(f"  Step 3: 生成计划书 ({len(validated_ideas)} 个通过验证的 idea)")

    tasks = []
    for item in validated_ideas:
        prompt = (
            "你是一位有丰富实验经验的 AI 研究者。请为以下通过同行评审的 idea 制定可执行计划书。\n\n"
            "=== Idea ===\n" + item.get("idea_text", "") + "\n===\n\n"
            "【硬件约束】\n" + resource_profile.load().describe() + "\n\n"
            "请输出极具执行力的计划（每一步都具体到可以直接执行）:\n\n"
            "## 预实验计划（第 1 周）\n"
            "目标: 用最小成本验证\"这个 idea 有没有信号\"。做完后能明确判断是否继续。\n\n"
            "1. 环境搭建:（哪个代码库、什么 Python 环境、如何配置）\n"
            "2. 数据准备:（数据集名称、下载命令、预处理步骤）\n"
            "3. 基线运行:（先跑一个 vanilla baseline 确认环境 OK）\n"
            "4. 核心验证实验:（最小改动实现核心 idea，跑一个小规模实验）\n"
            "5. 成功标准:（什么指标达到多少，或什么现象出现，算预实验通过）\n\n"
            "## 完整实验计划（第 2-4 周）\n"
            "1. 完整实现:（具体代码改动点）\n"
            "2. 消融实验:（必须做的消融，每个消融的目的）\n"
            "3. 对比实验:（与哪些 SOTA 对比，用哪些 benchmark）\n"
            "4. 分析实验:（可视化/case study/误差分析）\n"
            "5. 预期结果:（定量数字，论文贡献声明）"
        )
        tasks.append(prompt)

    results = _run_parallel(
        lambda prompt: call_role("planner", prompt, temperature=0.3), tasks)

    for item, result in zip(validated_ideas, results):
        if result:
            item["plan"] = result
            print(f"    ✅ {item.get('b_domain', '')} [{item.get('source_model', '')}]")
        else:
            item["plan"] = FAILURE_MARKER
            print("    ❌ 所有模型均失败")

    return validated_ideas


def resolve_directions(b_ids=None):
    """Directions for this run, resolved the one way names are resolved.

    run_idea_forge is reachable without going through the config, and it used to
    filter b_ids against the registry itself: an unknown name vanished, a repeated
    name ran twice, and a bare string iterated into characters and matched
    nothing.
    """
    return select_b_directions(b_ids)[0]


def run_idea_forge(seeds, b_ids=None, checkpoint_path=None):
    """
    Idea Forge 主流程
    seeds: 大浪淘沙输出的强推荐种子
    b_ids: 指定使用哪些领域方向（None = 全部）
    """
    require_idea_panel()
    b_library = resolve_directions(b_ids)
    repo_root = Path(__file__).parent.parent.parent
    explicit_checkpoint = checkpoint_path or os.getenv("AR_FORGE_CHECKPOINT")
    if explicit_checkpoint:
        output_file = Path(explicit_checkpoint)
        if not output_file.is_absolute():
            output_file = repo_root / output_file
    else:
        output_file = repo_root / "data" / "idea_forge" / (
            f"forge_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")

    run_config = {
        "idea_models": IDEA_MODELS,
        "plan_models": PLAN_MODELS,
        "b_ids": [direction["id"] for direction in b_library],
        "validation": "strict (顶会标准, >50% 通过)",
    }
    all_results = []
    if explicit_checkpoint and output_file.exists():
        saved = json.loads(output_file.read_text(encoding="utf-8"))
        saved_config = saved.get("config", {})
        for key in ("idea_models", "plan_models", "b_ids"):
            if saved_config.get(key) != run_config[key]:
                raise RuntimeError(f"checkpoint 的 {key} 与当前配置不同，拒绝混合两次运行")
        all_results = list(saved.get("results", []))

    print("════════════════════════════════════════════════════════════")
    print("  Idea Forge - 研究方案锻造")
    print(f"  研究信号: {len(seeds)}")
    print(f"  领域方向: {len(b_library)} ({', '.join(b['id'] for b in b_library)})")
    print(f"  模型: {', '.join(IDEA_MODELS)}")
    print(f"  当前最大并发请求: {llm_client.configured_max_concurrency()}（每批重新读取配置）")
    if all_results:
        print(f"  断点续跑: 已完成 {len(all_results)} 个种子")
    print("  目标: 顶会级别论文")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    completed = {_seed_key(record) for record in all_results}

    for i, seed in enumerate(seeds):
        if _seed_key(seed) in completed:
            print(f"\n[{i+1}/{len(seeds)}] 已完成，跳过: {seed.get('title', '')[:55]}")
            continue
        print(f"\n[{i+1}/{len(seeds)}] A: {seed.get('title', '')[:55]}")

        # 每颗种子都要留一条记录，哪怕它一关都没过。原来只记通过的，于是产出里
        # 「没产生 idea」和「产生了 3 个但全被否」长得一样，而这两件事要做的处理
        # 完全相反：前者说明构思那步坏了，后者说明它工作正常。
        record = {
            "seed_key": _seed_key(seed),
            "seed_title": seed.get("title", ""),
            "seed_url": seed.get("reddit_url") or seed.get("url", ""),
            "total_ideas": 0,
            "validated": 0,
            "plans": 0,
            "results": [],
        }

        # Step 1: 深度构思
        ideas = step1_deep_ideation(seed, b_library)
        record["total_ideas"] = len(ideas)
        if not ideas:
            print("  无有效 idea，跳过")
            record["stopped_at"] = "ideation"
            all_results.append(record)
            _save_checkpoint(output_file, run_config, all_results)
            continue

        # Step 2: 严格交叉验证
        validated = step2_strict_validation(ideas)
        record["validated"] = len(validated)
        if not validated:
            print("  无 idea 通过验证")
            record["stopped_at"] = "validation"
            # 被否的 idea 连同票数留下来，否则事后没法复核那几票是不是判对了。
            record["rejected"] = [
                {"idea_text": item.get("idea_text", ""),
                 "b_id": item.get("b_id", ""),
                 "validation": item.get("validation")}
                for item in ideas
            ]
            all_results.append(record)
            _save_checkpoint(output_file, run_config, all_results)
            continue

        # Step 2.5: 时新性刷新
        validated = step2_5_freshness_refresh(validated, enable_arxiv=True)

        # 共识检查（防撞 B 领域常识）
        consensus_passed = filter_by_consensus(validated)
        if not consensus_passed:
            print("  无 idea 通过共识检查")
            record["stopped_at"] = "consensus"
            all_results.append(record)
            _save_checkpoint(output_file, run_config, all_results)
            continue

        # Step 3: 计划书生成
        with_plans = step3_plan_generation(consensus_passed)

        record["plans"] = len([p for p in with_plans if has_usable_plan(p)])
        record["results"] = with_plans
        all_results.append(record)
        _save_checkpoint(output_file, run_config, all_results)

    output = _save_checkpoint(output_file, run_config, all_results)

    s = output["summary"]
    print("\n  Forge 完成")
    print(f"  生成 idea: {s['total_ideas']}")
    print(f"  通过验证: {s['total_validated']}")
    print(f"  产出计划: {s['total_plans']}")
    print(f"  保存: {output_file}")

    return output
