# ar-coordinator 启动与流程简述

## 启动方法

在 Claude Code 中运行：

```text
/ar-coordinator <idea_file> [project_root]
```

示例：

```text
/ar-coordinator ../data/ideas/example.txt
/ar-coordinator ../data/ideas/example.txt ../data/projects/example_project
```

参数说明：

- `idea_file`：必填，研究想法文件路径。
- `project_root`：可选，项目输出目录；不填时会自动生成到 `../data/projects/<slug>`。

## 启动后做什么

`ar-coordinator` 会先进入 Phase 0：

1. 读取 `idea_file`，解析研究想法。
2. 验证可选的知识方向来源，并把输入正文和 SHA256 固化到项目目录。
3. 创建或复用 `project_root`。
4. 初始化目录结构：
   - `idea.md`
   - `idea_provenance.json`
   - `plan.md`
   - `state.md`
   - `workflow_queue.json`
   - `decisions.log`
   - `code/`
   - `results/`
   - `review.md`
5. 启动 `ar-gemini-monitor.py` 监控 `results/run.log` 和 `results/summary.md`。
6. 自动配置 Ralph Loop，让工作流可以在每次停止后继续推进。

同一个 `project_root` 只能继续执行初始化时绑定的 idea。源文件或正文发生变化时，入口会拒绝
复用该项目；新实验使用新的项目目录。后续 agent 读取项目内的 `idea.md`，不会把来源元数据
当作研究指令。

## 主要处理流程

整体是一个可恢复的 AutoResearch 工作流，每轮只推进一个未完成单元。

典型队列如下：

1. `spawn_agents`：启动持久 agent：
   - `ar-planner`
   - `ar-coder`
   - `ar-runner`
2. `plan_gate`：planner 写 `plan.md`，reviewer 判断计划是否可执行。
3. `code_gate`：coder 根据计划写实验代码，reviewer 判断代码是否覆盖计划。
4. `code_review`：`ar-gemini-reviewer` 调 Gemini MCP 做代码审查。
5. `run_pilot_experiment`：runner 跑 Phase 1 预实验。
6. `pilot_result_analysis`：分析预实验结果，决定是否进入主实验。
7. `planner_scale_up`：如果预实验通过，planner 扩展为 Phase 2 主实验计划。
8. `code_main_experiment`：coder 补齐主实验代码。
9. `review_main_experiment`：审查主实验代码。
10. `run_main_experiment`：runner 跑主实验。
11. `main_result_analysis`：分析主实验结果。
12. `close_if_done`：全部完成后收尾并输出完成标记。

## Ralph Loop 续跑机制

Ralph Loop 会反复投递类似提示：

```text
继续 AutoResearch 工作流。读取 <project_root>/state.md 和 <project_root>/workflow_queue.json,只执行下一个未完成单元,写回状态。如果全部完成,最后一行输出 <promise>AUTORESEARCH_DONE</promise>。
```

coordinator 每次被唤醒时会：

1. 读取 `state.md` 和 `workflow_queue.json`。
2. 找到第一个 `pending` 或未完成单元。
3. 只执行这一个单元。
4. 写回 `state.md`、`workflow_queue.json` 和 `decisions.log`。
5. 如果还有待办，不输出完成标记。
6. 如果全部完成，最后输出：

```xml
<promise>AUTORESEARCH_DONE</promise>
```

## 各 agent 分工

- `ar-coordinator`：只负责调度、写状态、更新队列，不直接写代码、不直接跑实验。
- `ar-planner`：写或修订 `plan.md`。
- `ar-coder`：按计划在 `code/` 下写实验代码。
- `ar-subcoder`：被 coder 临时召唤，用于实现较独立的大模块。
- `ar-gemini-reviewer`：整理上下文并调用 Gemini MCP 做 gate 或 code review。
- `ar-runner`：创建项目 conda 环境、运行实验、debug、写 `results/summary.md`。
- `ar-gemini-monitor.py`：后台监控日志和 summary，写通知与状态。

## 关键状态文件

- `state.md`：当前状态快照，可恢复工作流的主要依据。
- `workflow_queue.json`：待执行单元队列。
- `decisions.log`：append-only 时间线。
- `results/run.log`：runner 运行日志。
- `results/summary.md`：实验结果摘要。
- `results/notifications.log`：monitor 通知。

## 完成条件

只有满足以下条件才算完成：

- `workflow_queue.json` 中所有单元都已完成。
- 主实验结果分析已完成，或明确记录了跳过 Phase 2 的原因。
- 没有 pending/running 的实验任务。
- 最后一行输出 `<promise>AUTORESEARCH_DONE</promise>`。
