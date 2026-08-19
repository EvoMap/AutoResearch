"""六条自组织抢占协议用例要真的被 CI 跑到。

`ar-runtime/scripts/tests/test_selforg_claim.py` 的入口叫 t1_* 到 t6_*，由自己的 main()
驱动，pytest 收集不到（实测 `pytest ar-runtime/scripts/tests` 是 no tests ran）。CI 注释
声称这六条已接入，实际 collection 里没有它们，所以这里用 subprocess 按它设计的方式跑一遍。

对比窗口期这份文件跑过 fork / ar-runtime 两份字节共享的拷贝；#108 cutover 后只剩一条
运行路径，跑一次就是全覆盖。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCRIPT = REPO / "ar-runtime" / "scripts" / "tests" / "test_selforg_claim.py"


def test_selforg_protocol_suite_passes():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (
        f"{SCRIPT.relative_to(REPO)} 非零退出（{proc.returncode}）：\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    assert "PASS" in proc.stdout, "脚本退出 0 但没有输出任何 PASS，检查它是否真的跑了用例"
