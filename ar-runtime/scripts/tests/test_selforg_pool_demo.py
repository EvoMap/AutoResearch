"""这个 demo 跑不跑得到底，由测试说了算。

`demo_selforg_pool.py` 是「4 worker 池比串行快」和「worker 崩溃后池自动接管」两个对外说法
的唯一证据来源。引擎给 result-analysis / critic / blind-review 三型单元加上裁决门之后
（#218），它在第一段串行基线就退 6 了，而四份文档照旧引着它，没有任何东西发现（#227）。

pytest 不收 `demo_*.py`，所以 CI 跑遍 `ar-runtime/scripts/tests/` 也碰不到它。要它别再悄悄
坏掉，得有一个测试真的把它执行一遍。

这里跑的是同一个 `main()`，只把每个单元的模拟工作时长调小：查的是协议路径通不通，墙钟
本测试只验证并行池输出的结构化不变量。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parent / "demo_selforg_pool.py"

# 三条裁决出口。demo 不认它们的名字，是引擎在 complete 被拒时把命令交回来的；这里按名字
# 断言，是为了确认那条路径真的被走过——DAG 哪天被改成不含裁决型单元，这个 gate 就名存实亡。
ADJUDICATED_EXITS = ["after-result-analysis", "after-critic", "after-blind-review"]


@pytest.fixture(scope="module")
def demo_run():
    env = {
        **os.environ,
        "AR_DEMO_WORK_SECONDS": "0.05",
        "AR_DEMO_CRASH_LEASE_SECONDS": "1",
    }
    return subprocess.run(
        [sys.executable, str(DEMO)], capture_output=True, text=True, env=env, timeout=600
    )


def test_the_demo_runs_to_the_end(demo_run):
    assert demo_run.returncode == 0, (
        f"demo 跑不到底（exit {demo_run.returncode}）：\n{demo_run.stdout}\n{demo_run.stderr}"
    )
    assert "RESULT:" in demo_run.stdout, demo_run.stdout


@pytest.mark.parametrize("scenario", ["D1 serial baseline", "D2 self-organizing pool", "D3 self-heal"])
def test_all_three_scenarios_report(demo_run, scenario):
    assert scenario in demo_run.stdout, f"{scenario} 没有跑出结果：\n{demo_run.stdout}"


@pytest.mark.parametrize("command", ADJUDICATED_EXITS)
def test_the_demo_follows_the_adjudicated_exits(demo_run, command):
    """回写路径认的是引擎给的出口，不是 demo 自己记的类型表。

    这三条命令出现在输出里，说明 demo 至少走过一次「complete 被拒 → 按返回里的 command
    改道」。断言的是行为不是实现：demo 里搜不到这三个字符串。
    """
    assert command in demo_run.stdout, (
        f"demo 没有走过 {command} 这条裁决出口：\n{demo_run.stdout}"
    )
