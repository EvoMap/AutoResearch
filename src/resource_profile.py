"""跑实验的机器有什么，只写一处。

硬件容量写死在提示词里会同时造成两类错误：换机器后计划与资源不符，公开模板还可能披露
部署方的真实基础设施。资源画像统一来自操作者声明或本机只读探测。

优先级是「声明 > 探测 > 说不知道」：

    配置里写了      用它。计划要跑在别的机器上时，本机探测出来的东西是错的。
    没写但探得到    用探测值。零配置的机器不该被迫先填一份表。
    都没有          说不知道，不编。提示词里出现一个没人验证过的硬件配置，
                    模型会照着它做规划，而那份规划从一开始就不可执行。

探测只读，不装任何东西：`nvidia-smi` 拿显卡，`os.cpu_count()` 和 `/proc/meminfo`
（Linux）或 `sysctl`（macOS）拿 CPU 与内存，`shutil.disk_usage` 拿磁盘。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import providers

REPO = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Resources:
    """一台机器上跟跑实验有关的东西。字段全是可选的：探不到就是探不到。"""

    gpu_count: int | None = None
    gpu_model: str = ""
    vram_gb: float | None = None
    cpu_cores: float | None = None
    memory_gb: float | None = None
    disk_gb: float | None = None
    weeks: float | None = None
    source: str = "unknown"          # declared | detected | unknown
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """写进提示词的那段。

        探不到就说探不到，不填缺省值。「未知」会让模型给出带条件的计划，而一个编出来
        的配置会让它给出一份看起来很具体、实际跑不了的计划。
        """
        if self.source == "unknown":
            return ("- 硬件配置未知（没有声明，也没探测到）\n"
                    "- 请给出对硬件规模不敏感的方案，或明确写出它需要什么规模")

        lines = []
        if self.gpu_count:
            gpu = f"- {self.gpu_count} 张 GPU"
            if self.gpu_model:
                gpu += f"（{self.gpu_model}"
                gpu += f"，{self.vram_gb:g}GB 显存）" if self.vram_gb else "）"
            elif self.vram_gb:
                gpu += f"（{self.vram_gb:g}GB 显存）"
            lines.append(gpu)
        else:
            lines.append("- 没有 GPU")
        if self.cpu_cores:
            lines.append(f"- {self.cpu_cores:g} 核 CPU"
                         + (f"，{self.memory_gb:g}GB 内存" if self.memory_gb else ""))
        if self.disk_gb:
            lines.append(f"- {self.disk_gb:g}GB 可用磁盘")
        if self.weeks:
            lines.append(f"- {self.weeks:g} 周时间")
        lines += [f"- {note}" for note in self.notes]
        if self.source == "detected":
            lines.append("- 以上为本机探测值；实验若在别的机器上跑，请在配置里声明")
        return "\n".join(lines)


def _gpus() -> tuple[int | None, str, float | None]:
    if not shutil.which("nvidia-smi"):
        return None, "", None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, "", None
    rows = [line for line in out.splitlines() if line.strip()]
    if not rows:
        return None, "", None
    name, _, mib = rows[0].partition(",")
    try:
        vram = round(float(mib.strip()) / 1024, 1)
    except ValueError:
        vram = None
    return len(rows), name.strip(), vram


def _memory_gb() -> float | None:
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=10, check=False).stdout.strip()
            return round(int(out) / 1024 ** 3, 1) if out.isdigit() else None
        except (OSError, subprocess.SubprocessError):
            return None
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    for line in meminfo.read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) > 1 and parts[1].isdigit():
                return round(int(parts[1]) / 1024 ** 2, 1)
    return None


def detect() -> Resources:
    """本机现在有什么。只读。"""
    count, model, vram = _gpus()
    try:
        free_gb = round(shutil.disk_usage(REPO).free / 1024 ** 3, 1)
    except OSError:
        free_gb = None
    return Resources(gpu_count=count, gpu_model=model, vram_gb=vram,
                     cpu_cores=os.cpu_count(), memory_gb=_memory_gb(),
                     disk_gb=free_gb, source="detected")


def from_config(config: dict[str, Any]) -> Resources | None:
    declared = config.get("resource_profile")
    if not isinstance(declared, dict) or not declared:
        return None
    known = {f for f in Resources.__dataclass_fields__ if f != "source"}
    return Resources(source="declared",
                     **{k: v for k, v in declared.items() if k in known})


def load(config: dict[str, Any] | None = None) -> Resources:
    """现在该按什么硬件规划。声明优先，其次探测，都没有就说不知道。"""
    if config is None:
        try:
            explicit = Path(os.environ["AUTORESEARCH_CONFIG"]).expanduser() \
                if os.environ.get("AUTORESEARCH_CONFIG") else None
            config = providers.load_effective_config(REPO, explicit)[0]
        except (OSError, ValueError):
            config = {}
    declared = from_config(config)
    if declared is not None:
        return declared
    found = detect()
    return found if found.gpu_count or found.cpu_cores else Resources()
