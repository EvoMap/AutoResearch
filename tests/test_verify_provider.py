from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import verify_provider  # noqa: E402

CONFIG = {
    "version": 2,
    "endpoints": {
        "gateway": {
            "dialect": "openai_chat",
            "base_url_env": "GATEWAY_BASE",
            "credential_env": ["GATEWAY_KEY"],
        },
        "azure": {
            "dialect": "openai_chat",
            "base_url_env": "AZURE_OPENAI_ENDPOINT",
            "credential_env": ["AZURE_KEY"],
        },
    },
    "models": {
        "gw-haiku": {"routes": [{"endpoint": "gateway", "wire_name": "claude-haiku"}]},
        "gw-glm": {"routes": [{"endpoint": "gateway", "wire_name": "glm-5.2"}]},
        "azure-ds": {"routes": [{"endpoint": "azure", "wire_name": "DeepSeek-V4"}]},
    },
    "roles": {"judge": {"models": ["gw-haiku", "gw-glm"]}},
}


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "providers.local.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_the_substitute_comes_from_the_same_endpoint(tmp_path: Path):
    assert verify_provider.pick_other_model(write(tmp_path, CONFIG), "gw-haiku") == "gw-glm"


def test_a_model_from_another_endpoint_is_not_a_substitute(tmp_path: Path):
    copy = json.loads(json.dumps(CONFIG))
    del copy["models"]["gw-glm"]
    assert verify_provider.pick_other_model(write(tmp_path, copy), "gw-haiku") == ""


def test_one_model_per_endpoint_is_not_a_defect(tmp_path: Path):
    copy = json.loads(json.dumps(CONFIG))
    copy["models"] = {"gw-haiku": copy["models"]["gw-haiku"]}
    assert verify_provider.pick_other_model(write(tmp_path, copy), "gw-haiku") == ""


def test_the_negative_control_is_routable(tmp_path: Path):
    cloned = verify_provider.clone_preset_with_bogus_model(write(tmp_path, CONFIG))
    config = json.loads(cloned.read_text(encoding="utf-8"))
    route = config["models"][verify_provider.BOGUS_MODEL]["routes"][0]
    assert route["wire_name"] == verify_provider.BOGUS_MODEL
    assert route["endpoint"] in CONFIG["endpoints"]


def test_it_does_not_invent_a_config(tmp_path: Path):
    assert verify_provider.clone_preset_with_bogus_model(write(tmp_path, {})) is None
    assert verify_provider.clone_preset_with_bogus_model(tmp_path / "missing.json") is None
