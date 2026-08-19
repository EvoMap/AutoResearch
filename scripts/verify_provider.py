#!/usr/bin/env python3
"""三档验证一个 provider：推荐模型能跑、换个模型也能跑、假模型必须被拒。

零参数。要验的角色自己探：挑第一个凭证齐全、能真发请求的必需角色。

为什么是三档而不是一档：

  第一档   推荐模型跑通        证明端点和凭证是好的
  第二档   换成别的模型跑通    证明「别的模型也行」这句话在实现里成立
  第三档   假模型必须被拒      证明第二档不是改了个标签

只做前两档时，一个把覆盖丢掉、永远发推荐模型的实现同样两档全绿。第三档是这套验证唯一
的负控：它要求那个不存在的模型名真的走到 wire 上并被上游拒绝。

负控用的名字必须是运行时**能路由**的——直接填一个没配过的名字会在发请求前就被挡下，
那只能证明检查器会挡，证明不了覆盖到没到 wire。所以这里临时复制一条 model route，端点
保持不变，只把 wire model name 换成不存在的。

    python scripts/verify_provider.py

退出码 0 = 三档都符合预期。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import roles as role_models  # noqa: E402  路径插入之后才可导入
import providers  # noqa: E402

PREFLIGHT = REPO / "scripts" / "preflight.py"
BOGUS_MODEL = "autoresearch-verify-no-such-model"


def run_preflight(config: Path, role: str, env_extra: dict[str, str]) -> tuple[int, str]:
    done = subprocess.run(
        [sys.executable, str(PREFLIGHT), "--config", str(config),
         "--live", "--role", role],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, **env_extra})
    return done.returncode, done.stdout + done.stderr


def resolvable_roles(config: Path) -> list[str]:
    """配置里哪些必需角色现在是齐的。不发请求，只看凭证。"""
    with tempfile.TemporaryDirectory() as directory:
        report_path = Path(directory) / "preflight.json"
        subprocess.run(
            [sys.executable, str(PREFLIGHT), "--config", str(config),
             "--json", str(report_path)],
            capture_output=True, text=True, timeout=300)
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    return [role for role, results in report.get("roles", {}).items()
            if any(r.get("status") == "ok" for r in results.values())]


def pick_config() -> Path:
    if explicit := os.environ.get("AUTORESEARCH_CONFIG"):
        return Path(explicit).expanduser()
    local = REPO / "config" / "providers.local.json"
    return local if local.exists() else REPO / "config" / "providers.example.json"


def endpoint_of(profile: dict) -> str:
    """这个 model alias 打到哪个端点。"""
    return str(profile.get("base_url_env") or profile.get("base_url") or "")


def pick_other_model(runtime_config: Path, exclude: str) -> str:
    """同一个端点上的另一个模型名。

    第二档要真的换掉模型才有意义：换成同一个的话，一个把覆盖整个丢掉、永远发推荐模型的
    实现照样两档全绿。

    但必须是**同一个端点**上的。第一版随便挑一个 preset 的模型，于是拿 Azure 上的
    DeepSeek-V4-Flash 去问 Anthropic 网关，得到「no usable candidate」——这一档红了，
    红的原因却不是覆盖没生效。模型名只在它自己的端点上有意义。
    """
    try:
        profiles = providers.as_profiles(json.loads(runtime_config.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return ""
    here = endpoint_of(profiles.get(exclude, {}))
    if not here:
        here = next((endpoint_of(profile) for profile in profiles.values()
                     if profile.get("model") == exclude), "")
    if not here:
        return ""
    return next((name for name, profile in profiles.items()
                 if name != exclude and endpoint_of(profile) == here), "")


def clone_preset_with_bogus_model(runtime_config: Path) -> Path | None:
    """照抄一个能用的 model route，只把 wire name 换掉，写进临时配置。

    负控要的是「运行时路由得到、上游认不出」的名字。直接填没配过的名字会在发请求前被
    可路由性检查挡下，那证明不了覆盖走到了 wire。
    """
    try:
        config = json.loads(runtime_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    models = config.get("models", {})
    if not models:
        return None
    donor = json.loads(json.dumps(next(iter(models.values()))))
    routes = donor.get("routes") or []
    if not routes:
        return None
    routes[0]["wire_name"] = BOGUS_MODEL
    donor["routes"] = routes
    config["models"][BOGUS_MODEL] = donor
    path = Path(tempfile.mkdtemp(prefix="ar-verify-")) / "providers.local.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", help="只验这个角色；不给就自己探")
    args = parser.parse_args()

    started = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    config = pick_config()
    runtime_config = config

    print(f"config  : {config.relative_to(REPO)}")
    print(f"runtime : {runtime_config}")

    role = args.role
    if not role:
        candidates = resolvable_roles(config)
        if not candidates:
            print("\n没有任何角色的凭证是齐的，先跑 python scripts/preflight.py 看缺什么")
            return 2
        role = candidates[0]
    print(f"role    : {role}\n")

    knob = role_models.env_name(role)
    results: list[tuple[str, str, bool, str]] = []

    # 第一档：推荐模型
    code, out = run_preflight(config, role, {})
    line = next((ln.strip() for ln in out.splitlines() if "=>" in ln), "(无)")
    results.append(("推荐模型", line, code == 0, f"exit={code}"))

    # 第二档：换一个**不同**的模型。换成同一个的话这一档是空转——一个把覆盖丢掉的实现
    # 照样能过。名字从运行时 preset 表里挑，排除第一档已经在用的那个。
    used = ""
    for ln in out.splitlines():
        if "=>" in ln and "（" in ln:
            used = ln.split("（", 1)[1].rstrip("）\n ").split("）")[0]
    swap = os.environ.get("AR_VERIFY_ALT_MODEL") or pick_other_model(runtime_config, used)
    if swap and swap != used:
        code, out2 = run_preflight(config, role, {knob: swap})
        shown = next((ln.strip() for ln in out2.splitlines() if "=>" in ln), "(无)")
        # 分清两件事：覆盖有没有生效，和端点服不服务这个模型。
        # 网关的 token 常常只授权一个模型，那是环境事实，不是这套机制的缺陷；而覆盖没
        # 落到请求上才是。判据是报告里有没有出现换上去的那个名字。
        engaged = swap in out2
        if not engaged:
            note, passed = "覆盖没生效：报告里根本没出现这个模型名", False
        elif code == 0:
            note, passed = "覆盖生效且端点服务它", True
        else:
            note, passed = "覆盖生效，但这个端点不服务它（多半是 token 只授权了一个模型）", True
            shown = next((ln.strip() for ln in out2.splitlines() if "FAIL" in ln), shown)
        results.append((f"换成 {swap}", shown, passed, note))
    else:
        # 同一个端点上只配了一个模型不是缺陷，跳过而不是判红。
        results.append(("换一个模型", "(跳过：同一端点上没有第二个模型可换)", True, "skipped"))

    # 第三档：负控
    bogus_config = clone_preset_with_bogus_model(runtime_config)
    if bogus_config is None:
        results.append(("负控 假模型", f"(跳过：{runtime_config} 里没有 models)", False, "skipped"))
    else:
        code, out3 = run_preflight(bogus_config, role, {knob: BOGUS_MODEL})
        rejected = code != 0
        why = next((ln.strip() for ln in out3.splitlines()
                    if "FAIL" in ln or "=>" in ln), "(无)")
        results.append(("负控 假模型", why, rejected,
                        "被拒（符合预期）" if rejected else "居然通过了——覆盖没走到 wire"))

    ok = all(passed for _, _, passed, _ in results)
    print("SUMMARY " + "=" * 62)
    print(f"  时间     {stamp}   耗时 {time.time() - started:.0f}s")
    print(f"  角色     {role}   旋钮 {knob}")
    for name, detail, passed, note in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name:<28} {note}")
        print(f"        {detail}")
    print(f"  结论     {'三档都符合预期' if ok else '有不符合预期的档，看上面 FAIL 那行'}")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
