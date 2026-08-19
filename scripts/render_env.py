#!/usr/bin/env python3
"""从 providers 配置生成消费者要读的 env，分两层。

发送方是两个 runtime（Python 和 Bun），共享库在结构上不可能，所以形态只能是「一份真相 +
生成的投影」。但投影有两种，风险面差得很远：

    tracked structural   settings.local.example.json、.env.example、consumer manifest
                         不含任何值，CI 能重生成并 diff
    ignored local        settings.local.json 等，含真实凭据，CI 看不见也不该看见

分开的理由是硬的：CI 对 ignored 文件做不了 diff，而把两者混成一个「重新生成并比对」的门，
要么门永远红、要么它其实什么都没比。

只接管显式列出的那些键。`settings.local.json` 里还有 `permissions`、`mcpServers`、
`enabledPlugins`，那些不是生成的——覆盖整个文件是最容易把别人机器搞坏的一件事。被接管的
键被手改成别的值时拒绝写入，除非 `--force`。

用法：
    python scripts/render_env.py --check      # 只比对 tracked 投影，不写任何文件
    python scripts/render_env.py              # 同上，外加按当前 env 刷新本机 ignored 文件
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import providers  # noqa: E402  路径插入之后才可导入
import roles  # noqa: E402

CONFIG = REPO / "config" / "providers.example.json"
SETTINGS_EXAMPLE = REPO / "ar-runtime" / ".claude" / "settings.local.example.json"
SETTINGS_LOCAL = REPO / "ar-runtime" / ".claude" / "settings.local.json"

# 这些键由本脚本负责，其余原样保留。名字写死在这里而不是从配置推导：投影要接管什么
# 是一个需要有人点头的决定，不该随配置改动悄悄扩大。
OWNED = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
)

# These keys were owned before reviewer and critic switched to the JSON role bridge. Leaving an old
# generated value in settings.local.json makes it override .env in the child process.
RETIRED_OWNED = (
    "GEMINI_BASE_URL", "GEMINI_API_KEY", "GEMINI_REVIEW_MODEL", "GEMINI_CRITIC_MODEL",
    "GEMINI_DEFAULT_SONNET_MODEL", "GEMINI_DEFAULT_OPUS_MODEL",
    "GPT_CRITIC_BASE_URL", "GPT_CRITIC_API_KEY", "GPT_CRITIC_MODEL",
)

# 谁要求哪个键是什么。列表能表达同一键的多个来源，`detect_conflicts` 才能拒绝歧义。
SOURCES: list[tuple[str, str, str]] = [
    ("ANTHROPIC_MODEL", "agent", "model"),
    ("ANTHROPIC_BASE_URL", "agent", "url_env"),
    ("ANTHROPIC_AUTH_TOKEN", "agent", "credential_env"),
    ("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "agent", "request_max_tokens"),
]


def explicit_config() -> Path | None:
    value = os.environ.get("AUTORESEARCH_CONFIG")
    return Path(value).expanduser() if value else None


class Conflict(RuntimeError):
    """两个消费者要往同一个键投不同的东西，或者被接管的键被手改过。"""


def load_config(layer: str = "local") -> dict[str, Any]:
    """两层各读各的输入，但都经同一个 loader。

    tracked 模板要发布，所以它必须由 tracked 配置生成——用本机生效配置生成的话，这台
    机器的端点变量名（GATEWAY_BASE、AZURE_OPENAI_ENDPOINT 之类）会漏进 git。

    The local projection must match preflight, so it uses the effective configuration: local
    values overlay the tracked defaults. Reading only the tracked layer previously let preflight
    validate one route while the generated official CLI settings selected another.
    """
    if layer == "tracked":
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    config, _, _ = providers.load_effective_config(REPO, explicit_config())
    return config


def owned_values(plan: dict[str, dict[str, str]], env: dict[str, str]) -> dict[str, str]:
    """这次生成会往本机那份里写的托管键值。

    新鲜度和真正的写入必须问同一处。上一版新鲜度问的是一份存下来的配置指纹，而投影的
    输出还取决于生成它的代码和当时的环境变量：#185 改了两侧共用的候选判据，配置一个
    字节没动，指纹一样，于是 bringup 报「一致」而文件已经不是现在会写出去的那份（#219）。
    """
    wanted: dict[str, str] = {}
    for key in OWNED:
        entry = plan.get(key)
        if entry is None:
            continue
        # 模型名是字面值；端点和凭据在配置里是变量名，取当前环境里的值。
        value = (entry["value"] if entry["axis"] in ("model", "request_max_tokens")
                 else env.get(entry["value"], ""))
        if value:
            wanted[key] = value
    return wanted


def local_is_fresh(env: dict[str, str] | None = None) -> tuple[bool, str]:
    """本机投影是不是现在会生成的那份。bringup 调它，不重新生成。

    现算一遍这次会写出去的值再比，所以三种 staleness 一次全覆盖：配置改了、环境变了、
    生成投影的代码升级了。
    """
    if not SETTINGS_LOCAL.exists():
        return False, "还没生成过，跑 python scripts/render_env.py"
    try:
        current = json.loads(SETTINGS_LOCAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "读不出来，跑 python scripts/render_env.py 重新生成"
    body = current.get("env", {})
    retired = sorted(key for key in RETIRED_OWNED if key in body)
    if retired:
        return False, f"这些废弃托管键仍会覆盖 .env：{', '.join(retired)}"
    try:
        plan = detect_conflicts(claims(load_config("local")))
    except (Conflict, OSError, ValueError) as exc:
        # bringup 只收一行，而 Conflict 是多行的。
        return False, f"算不出该写什么：{' '.join(str(exc).split())}"
    wanted = owned_values(plan, dict(os.environ) if env is None else env)
    previous = (current.get("_generated_from") or {}).get("values") or {}

    edited, behind = [], []
    for key in OWNED:
        was, value = body.get(key), wanted.get(key)
        if value is None:
            # 这次不会写它（配置里没有，或者它要的环境变量没设），只能问上次写完有没有被动过。
            if was and key in previous and was != previous[key]:
                edited.append(key)
        elif was == value:
            continue
        elif was and was != previous.get(key):
            edited.append(key)
        else:
            behind.append(key)
    if edited:
        return False, f"这些托管键被改过：{', '.join(edited)}"
    if behind:
        return False, (f"这些键不是现在会生成的值：{', '.join(behind)}，"
                       "跑 python scripts/render_env.py 重新生成")
    return True, "与现在会生成的结果一致"


def claims(config: dict[str, Any], *, use_env: bool = True) -> list[dict[str, str]]:
    """每一条「谁要求哪个键是什么」。列表而不是字典——字典存不下两个来源要求同一个键，
    于是冲突检测永远不会触发，成了一道死门。"""
    profiles = providers.as_profiles(config)
    # tracked 模板是给所有人的，不能随生成它的那台机器的环境变。`use_env=False` 时按
    # 配置里的原始顺序取首选——同一份配置在任何机器上算出同一份模板，CI 才 diff 得动。
    found: list[dict[str, str]] = []
    for key, role, axis in SOURCES:
        # 经 roles.models_for 而不是直接读配置：`AR_MODEL_<ROLE>` 在那里生效。
        # Reading the config directly would let preflight honor AR_MODEL_AGENT while the
        # generated settings.local.json retained the old model. The official CLI consumes the
        # projection, so both paths must ask the same resolver.
        # 用「现在真能用的」那批，不是配置里的原始顺序。preflight 会跳过配不通的候选
        # 去试下一个，投影原来直接取 `[0]`：单端点机器上 preflight 报 all resolved，而
        # 投影把第一个 Gemini route 写给了 Claude Code，MCP 因为 GEMINI_BASE_URL 没设
        # 而起不来（#135）。两侧现在问同一个解析器。
        usable = (providers.usable_candidates(config, role) or
                  providers.role_candidates(config, role)) if use_env \
            else providers.role_candidates(config, role)
        # `use_env=False` 时把空环境也传给 models_for：只跳过 usable_candidates 不够，
        # 它自己会读 os.environ 里的 AR_MODEL_<ROLE>，于是设了覆盖的机器生成出来的
        # tracked 模板与别人不同，CI 的 diff 判据就不成立了。
        candidates = roles.models_for(role, usable, env={} if not use_env else None)
        if candidates and candidates[0] not in profiles and os.environ.get(f"AR_MODEL_{role.upper()}"):
            # 名字是人从环境变量给的，解析不出来要说出来。静默丢键的后果是投影少写一个
            # 键、消费者退回它自己的默认值，而操作者以为覆盖生效了。preflight 对同样的
            # 输入是响亮报错的，两边要一致。
            raise Conflict(
                f"AR_MODEL_{role.upper()}={candidates[0]!r} 在配置里找不到。\n"
                f"  现有：{', '.join(sorted(profiles))}")
        profile = profiles.get(candidates[0]) if candidates else None
        if not profile:
            continue
        if axis == "model":
            value = str(profile.get("model") or "")
            source = f"role {role} 的首选模型"
        elif axis == "request_max_tokens":
            value = str(providers.request_params(config, role).get("max_tokens") or "")
            source = f"role {role} 的请求预算"
        elif axis == "url_env":
            value = str(profile.get("base_url_env") or profile.get("base_url") or "")
            source = f"role {role} 的端点"
        else:
            value = str(profile.get("api_key_env") or profile.get("auth_token_env") or "")
            source = f"role {role} 的凭据变量"
        if value:
            found.append({"key": key, "value": value, "from": source, "axis": axis})
    return found


def detect_conflicts(found: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """同一个键被两处要求成不同的值时说清是谁跟谁打架；没冲突就收敛成计划。"""
    by_key: dict[str, list[dict[str, str]]] = {}
    for claim in found:
        by_key.setdefault(claim["key"], []).append(claim)

    bad = {key: entries for key, entries in by_key.items()
           if len({e["value"] for e in entries}) > 1}
    if bad:
        raise Conflict("同一个 env 键被要求成不同的值：\n  " + "\n  ".join(
            f"{key}: " + "，".join(f"{e['value']}（来自 {e['from']}）" for e in entries)
            for key, entries in bad.items()))
    return {key: entries[0] for key, entries in by_key.items()}


def render_example(plan: dict[str, dict[str, str]], current: dict[str, Any]) -> dict[str, Any]:
    """tracked 的模板：只接管模型名，端点和凭据保留原来的填写提示。

    Do not write `${VAR}` here: the official CLI treats it as a literal settings value rather
    than expanding it, which would send requests to the placeholder text.

    模型名是字面值、不含凭据，所以它可以也应该由配置驱动——那正是「换模型只改一处」
    要买的东西。端点和凭据仍然是人填，模板只负责说清填什么。
    """
    rendered = json.loads(json.dumps(current))
    env = rendered.setdefault("env", {})
    for key in RETIRED_OWNED:
        env.pop(key, None)
    for key in OWNED:
        entry = plan.get(key)
        if entry is not None and entry["axis"] in ("model", "request_max_tokens"):
            env[key] = entry["value"]
    return rendered


def write_atomically(path: Path, text: str, mode: int = 0o600) -> None:
    """原子写 + 收紧权限。半截文件比没有文件更糟：读的人会拿它当完整的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8")
    try:
        handle.write(text)
        handle.close()
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise


def materialise(plan: dict[str, dict[str, str]], env: dict[str, str],
                force: bool) -> tuple[dict[str, str], list[str]]:
    """本机那份：把变量名换成当前环境里的值，其余键原样保留。"""
    current: dict[str, Any] = {}
    if SETTINGS_LOCAL.exists():
        current = json.loads(SETTINGS_LOCAL.read_text(encoding="utf-8"))
    existing = current.get("env", {})

    # 三方比较：现在的值、上次生成的值、这次要写的值。记下上次写出去的值，「这个值是
    # 投影写的还是人写的」才分得开，否则配置正常升级也会被当成手改而要求 --force，
    # 而 --force 会连真手改一起盖。
    previous = (current.get("_generated_from") or {}).get("values", {})

    changed, refused = [], []
    for key in RETIRED_OWNED:
        if key not in existing:
            continue
        was = existing[key]
        if previous.get(key) == was or force:
            del existing[key]
            changed.append(key)
        else:
            refused.append(
                f"{key}：已不再由投影管理，但仍会覆盖 .env。确认后删除或加 --force")
    for key, value in owned_values(plan, env).items():
        was = existing.get(key)
        # 判据是「现在的值等不等于上次生成的值」：相等说明没人动过，差异来自配置，跟上；
        # 不等说明有人改过，不管配置有没有升级都不能悄悄盖。
        if was and was != value and was != previous.get(key) and not force:
            refused.append(f"{key}：本机是 {was!r}，上次生成的是 "
                           f"{previous.get(key)!r}，有人改过。确认后加 --force")
            continue
        if was != value:
            changed.append(key)
        existing[key] = value

    current["env"] = existing
    current["_generated_from"] = {
        "script": Path(__file__).name,
        # 记下这次写出去的托管值，下次才判得出「有人改过」。新鲜度不看这里，它现算
        # 一遍再比——存下来的东西答不了「生成它的代码变没变」。
        "values": {key: existing[key] for key in OWNED if key in existing},
    }
    if refused:
        raise Conflict("这些被接管的键在本机被手改过：\n  " + "\n  ".join(refused))
    return current, changed



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只比对 tracked 投影，不写文件")
    parser.add_argument("--force", action="store_true", help="覆盖被手改过的托管键")
    args = parser.parse_args()

    try:
        tracked_plan = detect_conflicts(claims(load_config("tracked"), use_env=False))
        local_plan = detect_conflicts(claims(load_config("local")))
    except Conflict as exc:
        print(exc, file=sys.stderr)
        return 2
    plan = local_plan

    print(f"接管 {len(plan)}/{len(OWNED)} 个键：")
    for key, entry in sorted(plan.items()):
        print(f"  {key:24} <- {entry['value']:28} （{entry['from']}）")

    current = json.loads(SETTINGS_EXAMPLE.read_text(encoding="utf-8"))
    # 模板从 tracked 配置生成，本机那份从生效配置生成。
    wanted = json.dumps(render_example(tracked_plan, current),
                        ensure_ascii=False, indent=2) + "\n"
    stale = wanted != SETTINGS_EXAMPLE.read_text(encoding="utf-8")

    if args.check:
        if stale:
            print(f"\n{SETTINGS_EXAMPLE.relative_to(REPO)} 与配置不一致，"
                  "跑 python scripts/render_env.py 重新生成", file=sys.stderr)
            return 1
        print("\ntracked 投影与配置一致")
        return 0

    if stale:
        SETTINGS_EXAMPLE.write_text(wanted, encoding="utf-8")
        print(f"\n更新 {SETTINGS_EXAMPLE.relative_to(REPO)}")

    try:
        materialised, changed = materialise(plan, dict(os.environ), args.force)
    except Conflict as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    write_atomically(SETTINGS_LOCAL,
                     json.dumps(materialised, ensure_ascii=False, indent=2) + "\n")
    print(f"写入 {SETTINGS_LOCAL.relative_to(REPO)}（0600），"
          f"{len(changed)} 个键有变化，其余键原样保留")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
