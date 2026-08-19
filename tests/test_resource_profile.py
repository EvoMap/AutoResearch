"""跑实验的机器有什么，只写一处，不在公开模板里预填部署容量。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import resource_profile as rp  # noqa: E402

SYNTHETIC = {"resource_profile": {
    "gpu_count": 2, "gpu_model": "Example Accelerator",
    "vram_gb": 24, "cpu_cores": 16, "memory_gb": 64, "disk_gb": 200, "weeks": 2}}


def test_a_declared_profile_wins_over_what_this_machine_has():
    """计划要跑在别的机器上时，本机探测出来的东西是错的。"""
    described = rp.load(SYNTHETIC).describe()

    assert "2 张 GPU" in described and "24GB 显存" in described
    assert "Example Accelerator" in described


def test_a_machine_with_no_declaration_uses_what_it_can_see():
    """零配置的机器不该被迫先填一份表。"""
    found = rp.load({})

    assert found.source in ("detected", "unknown")
    if found.source == "detected":
        assert found.cpu_cores, "探测到了却什么都没填"


def test_nothing_known_says_so_instead_of_inventing():
    """提示词里出现一个没人验证过的配置，模型会照着它做规划，而那份规划跑不了。"""
    described = rp.Resources().describe()

    assert "未知" in described
    for invented in ("64 张", "Invented Accelerator", "512GB", "12 周"):
        assert invented not in described


def test_a_detected_profile_says_it_was_detected():
    """本机探测值和声明值必须能分开：前者换台机器就变。"""
    assert "本机探测值" in rp.detect().describe()
    assert "本机探测值" not in rp.load(SYNTHETIC).describe()


def test_detection_never_installs_or_writes_anything():
    """探测在别人的机器上跑，只读。"""
    import inspect

    source = inspect.getsource(rp)
    for forbidden in ("pip install", "write_text", "mkdir", "os.remove", "shutil.rmtree"):
        assert forbidden not in source, f"探测里有 {forbidden}"


@pytest.mark.parametrize("prompt_builder", ["领域交叉构思", "计划书"])
def test_neither_forge_prompt_hardcodes_hardware(prompt_builder):
    """两处都不能再写死。判据是文件里没有那串字面量，不是「有个函数叫这个名」。"""
    body = (REPO / "src" / "idea_forge" / "forge.py").read_text(encoding="utf-8")

    assert body.count("resource_profile.load().describe()") == 2, \
        "两个提示词都要读同一处"


def test_the_example_config_shows_where_to_declare_it():
    """新机器要知道去哪填。"""
    import json

    example = json.loads((REPO / "config.example.json").read_text(encoding="utf-8"))

    assert "resource_profile" in example
    assert example["resource_profile"].get("_note"), "要说清不填会怎样"
    profile = {key: value for key, value in example["resource_profile"].items()
               if not key.startswith("_")}
    assert all(value is None or value == [] for value in profile.values()), \
        "公开示例必须是空模板，不能复制某台真实机器的容量"


def test_the_profile_survives_a_round_trip_through_the_config():
    """声明的字段要真的到达提示词，不是被 dataclass 静默丢掉。"""
    described = rp.from_config(SYNTHETIC).describe()

    for expected in ("16 核", "64GB 内存", "200GB 可用磁盘", "2 周"):
        assert expected in described, f"{expected} 没到提示词里"


def test_an_unknown_field_in_the_config_does_not_crash():
    """配置是人手写的，多一个字段不该炸。"""
    profile = rp.from_config({"resource_profile": {"gpu_count": 2, "网卡": "100G"}})

    assert profile.gpu_count == 2
