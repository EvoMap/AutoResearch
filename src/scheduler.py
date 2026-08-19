"""
AutoResearch 定时调度器
- 首次运行：查前 1 个月数据
- 此后每天运行：只查增量（新增的）
- 结果按日存储，网页可查历史
- 储备池 15 天过期自动清除
"""

import json
import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DATA_DIR, CANDIDATES_DIR

STATE_FILE = DATA_DIR / "scheduler_state.json"


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_run": None, "first_run_done": False, "runs": []}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_first_run():
    state = load_state()
    return not state.get("first_run_done", False)


def mark_run_complete(today):
    state = load_state()
    state["last_run"] = today
    state["first_run_done"] = True
    if today not in state.get("runs", []):
        state.setdefault("runs", []).append(today)
    save_state(state)


def get_lookback_days():
    """首次 30 天，之后 1 天"""
    if is_first_run():
        return 30
    return 1


def clean_reserve_pool():
    """清除储备池中超过 15 天没有起色的条目"""
    reserve_file = CANDIDATES_DIR / "reserve_pool.json"
    if not reserve_file.exists():
        return 0

    with open(reserve_file) as f:
        pool = json.load(f)

    now = datetime.now()
    active = []
    expired = 0
    for item in pool:
        added = item.get("added_to_reserve", "")
        if added:
            try:
                added_dt = datetime.fromisoformat(added)
                if (now - added_dt).days > 15:
                    expired += 1
                    continue
            except (ValueError, TypeError, KeyError):
                # Malformed entry; a bare except here also swallowed
                # KeyboardInterrupt and SystemExit.
                pass
        active.append(item)

    with open(reserve_file, "w") as f:
        json.dump(active, f, ensure_ascii=False, indent=2)

    return expired


def run_daily():
    """每日执行入口"""
    today = datetime.now().strftime("%Y%m%d")
    lookback = get_lookback_days()

    print(f"\n{'═' * 60}")
    print(f"  AutoResearch 每日运行 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  模式: {'首次运行（查前30天）' if lookback == 30 else '增量更新（查昨日新增）'}")
    print(f"{'═' * 60}")

    # 清理过期储备
    expired = clean_reserve_pool()
    if expired:
        print(f"  储备池清理: 移除 {expired} 条过期条目")

    # 运行主 pipeline
    # 使用 pipeline_v4 的社区讨论方式（time_filter 根据是否首次来定）
    os.environ["AUTORESEARCH_LOOKBACK_DAYS"] = str(lookback)
    os.environ["AUTORESEARCH_DATE"] = today

    # 调用 pipeline
    pipeline_script = Path(__file__).parent / "pipeline_v4.py"
    result = subprocess.run(
        [sys.executable, str(pipeline_script)],
        cwd=str(Path(__file__).parent.parent),
        capture_output=False,
    )

    # 标记完成
    mark_run_complete(today)
    print(f"\n  运行完成，标记日期: {today}")

    return result.returncode


if __name__ == "__main__":
    run_daily()
