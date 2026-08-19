"""A project root has one monitor writer even when two launchers race."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


MONITOR = Path(__file__).resolve().parent.parent / "ar-gemini-monitor.py"


def test_two_monitors_for_one_project_root_leave_one_writer(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    watch = results / "run.log"
    notify = results / "notifications.log"
    state = results / "monitor_state.json"
    summary = results / "summary.md"
    common = [
        sys.executable,
        str(MONITOR),
        "--watch",
        str(watch),
        "--notify-log",
        str(notify),
        "--project-root",
        str(tmp_path),
        "--state",
        str(state),
        "--summary",
        str(summary),
        "--interval",
        "1",
    ]
    first = subprocess.Popen(common, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(50):
            if notify.exists() and "event=monitor_started" in notify.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)

        second = subprocess.run([*common, "--once"], capture_output=True, text=True, timeout=10)

        assert second.returncode == 0, second.stderr
        assert "already running" in second.stderr.lower()
        assert notify.read_text(encoding="utf-8").count("event=monitor_started") == 1
        assert first.poll() is None
    finally:
        if first.poll() is None:
            first.terminate()
        first.wait(timeout=10)
