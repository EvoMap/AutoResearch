#!/usr/bin/env python3
"""自组织抢占协议(claim/lease/reclaim)的并发与语义测试。

直接以子进程方式调用 ar-workflow-engine.py，模拟真实多 worker 场景：
 T1 并发抢占：8 worker × 12 独立单元，每单元恰好被抢一次
 T2 租约过期回收：失联 worker 的单元被别人收回，原 worker 迟到回写被拒
 T3 毒丸保护：同一单元被回收 3 次后标 blocked
 T4 DAG 门控：依赖未满足的单元抢不到，blocker 完成后立刻可抢
 T5 类型过滤：--types 只抢指定类型
 T6 队列完整性：全程 workflow_queue.json 可解析、计数一致
"""

import json
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ENGINE = str(Path(__file__).resolve().parent.parent / "ar-workflow-engine.py")


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, ENGINE, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"engine failed: {args}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def make_project(units: list[dict]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ar_claim_test_"))
    queue = {"mode": "autoresearch_loop", "current_cycle": 0, "max_cycles": 3, "units": units}
    (root / "workflow_queue.json").write_text(json.dumps(queue, ensure_ascii=False, indent=2))
    return root


def worker_loop(project_root: str, worker: str) -> list[str]:
    """真实 worker 行为：循环 claim → 干活(短暂 sleep) → complete，直到队列空。"""
    claimed: list[str] = []
    while True:
        out = json.loads(
            run(["claim", "--project-root", project_root, "--worker", worker]).stdout
        )
        if out["status"] == "empty":
            return claimed
        unit_id = out["unit"]["id"]
        claimed.append(unit_id)
        time.sleep(0.05)
        run([
            "complete", "--project-root", project_root, "--unit", unit_id,
            "--worker", worker, "--status", "done", "--result", f"by_{worker}",
        ])


def t1_concurrent_claims() -> None:
    units = [{"id": f"u{i:02d}", "type": "coding", "status": "pending"} for i in range(12)]
    root = make_project(units)
    with ProcessPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker_loop, str(root), f"w{i}") for i in range(8)]
        results = [f.result() for f in futures]
    all_claimed = [u for r in results for u in r]
    assert sorted(all_claimed) == sorted(u["id"] for u in units), (
        f"double-claim or lost unit: {sorted(all_claimed)}"
    )
    assert len(all_claimed) == len(set(all_claimed)), f"double-claim detected: {all_claimed}"
    queue = json.loads((root / "workflow_queue.json").read_text())
    done = [u for u in queue["units"] if u["id"].startswith("u") and u["status"] == "done"]
    assert len(done) == 12, f"expected 12 done, got {len(done)}"
    workers_used = {u.get("result") for u in done}
    print(f"  T1 PASS: 12 units, 8 workers, zero double-claims, {len(workers_used)} distinct workers did work")


def t2_lease_reclaim_and_late_write() -> None:
    root = make_project([{"id": "solo", "type": "run", "status": "pending"}])
    out = json.loads(
        run(["claim", "--project-root", str(root), "--worker", "dead_worker", "--lease-seconds", "1"]).stdout
    )
    assert out["status"] == "claimed"
    time.sleep(1.5)
    out2 = json.loads(run(["claim", "--project-root", str(root), "--worker", "rescuer"]).stdout)
    assert out2["status"] == "claimed" and out2["unit"]["id"] == "solo", out2
    assert "solo" in out2["reclaimed"], f"expected reclaim log, got {out2['reclaimed']}"
    late = run(
        ["complete", "--project-root", str(root), "--unit", "solo", "--worker", "dead_worker"],
        check=False,
    )
    assert late.returncode == 3 and "claim_lost" in late.stdout, (
        f"late write must be rejected: rc={late.returncode} out={late.stdout}"
    )
    log = (root / "decisions.log").read_text()
    assert "lease_reclaimed" in log and "lost_worker=dead_worker" in log
    print("  T2 PASS: expired lease reclaimed by rescuer; dead worker's late write rejected (claim_lost)")


def t3_poison_unit() -> None:
    root = make_project([{"id": "poison", "type": "run", "status": "pending"}])
    for i in range(3):
        out = json.loads(
            run(["claim", "--project-root", str(root), "--worker", f"crash{i}", "--lease-seconds", "1"]).stdout
        )
        assert out["status"] == "claimed", f"round {i}: {out}"
        time.sleep(1.2)
    out = json.loads(run(["claim", "--project-root", str(root), "--worker", "next"]).stdout)
    assert out["status"] == "empty", f"poison unit must not be claimable: {out}"
    queue = json.loads((root / "workflow_queue.json").read_text())
    unit = next(u for u in queue["units"] if u["id"] == "poison")
    assert unit["status"] == "blocked" and "reclaimed_3_times" in unit.get("blocked_reason", ""), unit
    assert "unit_poisoned" in (root / "decisions.log").read_text()
    print("  T3 PASS: unit reclaimed 3x auto-blocked (poison protection), queue reports empty not livelock")


def t4_dag_gating() -> None:
    root = make_project([
        {"id": "a", "type": "planning", "status": "pending"},
        {"id": "b", "type": "coding", "status": "pending", "blocked_by": "a"},
        {"id": "c", "type": "coding", "status": "pending", "blocked_by": ["a", "b"]},
    ])
    out = json.loads(run(["ready", "--project-root", str(root)]).stdout)
    assert out["width"] == 1 and out["ready"][0]["id"] == "a", out
    out = json.loads(run(["claim", "--project-root", str(root), "--worker", "w"]).stdout)
    assert out["unit"]["id"] == "a"
    out = json.loads(run(["claim", "--project-root", str(root), "--worker", "w2"]).stdout)
    assert out["status"] == "empty", f"b must stay gated while a runs: {out}"
    run(["complete", "--project-root", str(root), "--unit", "a", "--worker", "w"])
    out = json.loads(run(["claim", "--project-root", str(root), "--worker", "w2"]).stdout)
    assert out["status"] == "claimed" and out["unit"]["id"] == "b", out
    print("  T4 PASS: DAG gating holds under claim mode; successors unlock immediately on complete")


def t5_type_filter() -> None:
    root = make_project([
        {"id": "code1", "type": "coding", "status": "pending"},
        {"id": "run1", "type": "run", "status": "pending"},
    ])
    out = json.loads(
        run(["claim", "--project-root", str(root), "--worker", "runner", "--types", "run"]).stdout
    )
    assert out["status"] == "claimed" and out["unit"]["id"] == "run1", out
    print("  T5 PASS: --types filter routes the right unit to a specialized worker")


def t6_queue_integrity() -> None:
    units = [{"id": f"m{i}", "type": "coding", "status": "pending"} for i in range(6)]
    root = make_project(units)
    with ProcessPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(worker_loop, str(root), f"w{i}") for i in range(6)]
        for f in futures:
            f.result()
    queue = json.loads((root / "workflow_queue.json").read_text())
    assert queue["schema_version"] == 1
    assert queue["iteration"] >= 6
    for u in queue["units"]:
        assert "lease_expires_at" not in u, f"lease not cleaned on complete: {u}"
    print("  T6 PASS: queue JSON valid after churn, iteration counted, leases cleaned on completion")


def main() -> None:
    tests = [t1_concurrent_claims, t2_lease_reclaim_and_late_write, t3_poison_unit,
             t4_dag_gating, t5_type_filter, t6_queue_integrity]
    print(f"self-organizing claim protocol tests ({len(tests)} cases)")
    start = time.time()
    for t in tests:
        t()
    print(f"ALL PASS in {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
