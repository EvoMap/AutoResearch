#!/usr/bin/env python3
"""调用点提到的每个模型、角色和 env，providers 配置里都要有对应的一条。

这是配置统一的第一道门，也是唯一一道在重构开始之前就该立起来的。目标形态是「角色是
接口，模型是数据」：调用点只说用途，模型写在配置里。今天离这个形态还很远，所以这个门
不要求立刻全绿，它要求的是**只减不增**。

为什么扫调用点而不是字符串字面量：`llm_client.py` 里模型名形状的字面量有十几处，其中
有提示词里的、注释里的、以及像 `"claude"` 这样的路由分支标记。按字面量扫会产出一份混着
误报的清单，而按这个仓库自己的经验（`ruff.toml` 里那段），没人能弄绿的门会被关掉。

调用点是有限且确定的：

  call_model 的 _LEGACY 分派名   这是 Python 侧「角色 → 模型」的真实接口
  get_preset("字面量")           直接按名字取配置的地方
  ROLE_DEFAULTS 的兜底模型        配置读不出来时每个角色用谁
  roles.models_for(角色, 推荐)    推荐值写在调用参数里的形态
  两个 MCP 的 process.env.X      Bun 侧的 env 回退链

退出码 0 表示没有新增未覆盖项，1 表示有新增或有陈旧豁免。
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "providers.example.json"

sys.path.insert(0, str(REPO / "src"))

import providers  # noqa: E402  路径插入之后才可导入

PY_SOURCES = [
    REPO / "src" / "llm_client.py",
    REPO / "src" / "idea_forge" / "forge.py",
    REPO / "src" / "idea_forge" / "freshness.py",
]
TS_SOURCES = [
    REPO / "ar-runtime" / "scripts" / "ar-gemini-review-mcp.ts",
    REPO / "ar-runtime" / "scripts" / "ar-external-critic-mcp.ts",
]

# 今天已知还没被配置覆盖的引用。这份清单只允许变短：新增会让门变红，而这里留着一条
# 实际已经覆盖了的，同样让门变红：否则清单会慢慢变成一份没人维护的免罪符。
#
# 每条后面是清理它的交付步骤。MCP 专用 env 已由统一角色入口清除。
EXPECTED_UNRESOLVED = {
    # Step 2：call_model 的分派名。改成按角色读 roles 之后这三个会消失。
    "model:gemini-flash",       # llm_client._LEGACY
    "model:claude-sonnet",      # llm_client._LEGACY
    "model:claude-haiku",       # llm_client._LEGACY
    # MCP 已经通过 scripts/call_role.py 读取同一份角色配置，不再保留 provider 专用 env。
}


def declared() -> tuple[set[str], set[str], set[str]]:
    """配置里有哪些 profile、env 和 role 名。"""
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    # 走读取器：v2 里 profile 名叫 model 名，直接读 `profiles` 会拿到空集合，于是
    # 每个引用都变成「未声明」，而门的输出看起来像是代码引用了不存在的模型。
    profiles = set(providers.as_profiles(config))
    envs: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith(("_env", "_env_fallback")) and isinstance(value, str):
                    envs.add(value)
                # v2 的 `credential_env` / `url_env_fallback` 是列表：同一个端点上
                # 合并出来的候选变量。只认字符串的话这些名字集体消失。
                if key.endswith(("_env", "_env_fallback")) and isinstance(value, list):
                    envs.update(v for v in value if isinstance(v, str))
                if key in ("model_env_chain", "family_from_env") and isinstance(value, list):
                    envs.update(v for v in value if isinstance(v, str))
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(config)
    return profiles, envs, set(config.get("roles", {}))


def model_references() -> dict[str, list[str]]:
    """Python 调用点提到的模型名 -> 出现位置。"""
    found: dict[str, list[str]] = {}

    def record(name, where):
        found.setdefault(name, []).append(where)

    for path in PY_SOURCES:
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO)
        tree = ast.parse(source)

        for node in ast.walk(tree):
            # 分派比较式：`model_name == "gemini-pro"`（call_model 之外的地方还有）
            if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.Eq):
                left = node.left
                right = node.comparators[0]
                if (isinstance(left, ast.Name) and left.id in ("model_name", "model")
                        and isinstance(right, ast.Constant) and isinstance(right.value, str)):
                    record(right.value, f"{rel}:{node.lineno}")

            # get_preset("字面量")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "get_preset" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                record(node.args[0].value, f"{rel}:{node.lineno}")

            # _LEGACY = {"claude-opus": ...}：call_model 的老名字派发表。
            # 它以前是 `if model_name == "..."` 的链，扫 Compare 就够；改成 dict 之后
            # 扫描器一声不响地少看见三个名字，清单反而"变短"了。门变绿的原因是它瞎了。
            if isinstance(node, ast.Assign) and any(
                    isinstance(x, ast.Name) and x.id == "_LEGACY" for x in node.targets):
                for key in getattr(node.value, "keys", []):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        record(key.value, f"{rel}:{node.lineno}")

            # ROLE_DEFAULTS = {"角色": "模型"} 或 {"角色": ["模型", ...]}：配置读不出来
            # 时的兜底表。模型名从 forge 搬进它之后（#190），这是 Python 侧唯一还写着模型
            # 名的地方；不扫它，门会因为自己瞎了而变绿。
            if isinstance(node, ast.Assign) and any(
                    isinstance(x, ast.Name) and x.id == "ROLE_DEFAULTS" for x in node.targets):
                for value in getattr(node.value, "values", []):
                    for item in ([value] if isinstance(value, ast.Constant)
                                 else getattr(value, "elts", [])):
                        if isinstance(item, ast.Constant) and isinstance(item.value, str):
                            record(item.value, f"{rel}:{node.lineno}")

            # roles.models_for("<角色>", <推荐模型>)：推荐值直接写在调用参数里的形态。
            # 按常量名扫（IDEA_MODELS / PLAN_MODEL）会随着模型名搬进调用参数而漏掉，
            # 于是门照样绿。这次就是这么红的。
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", getattr(node.func, "id", None)) == "models_for"
                    and len(node.args) >= 2):
                arg = node.args[1]
                for value in ([arg] if isinstance(arg, ast.Constant)
                              else getattr(arg, "elts", [])):
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        record(value.value, f"{rel}:{node.lineno}")

            # IDEA_MODELS / PLAN_MODEL，以及默认值形态的 refresher_model="claude-opus"
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("IDEA_MODELS", "PLAN_MODEL"):
                        for value in ([node.value] if isinstance(node.value, ast.Constant)
                                      else getattr(node.value, "elts", [])):
                            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                                record(value.value, f"{rel}:{node.lineno}")
            if isinstance(node, ast.FunctionDef):
                for name, default in zip(
                        node.args.args[-len(node.args.defaults):] if node.args.defaults else [],
                        node.args.defaults):
                    if (name.arg.endswith("_model") and isinstance(default, ast.Constant)
                            and isinstance(default.value, str)):
                        record(default.value, f"{rel}:{node.lineno}")
    return found


def env_references() -> dict[str, list[str]]:
    """两个 MCP 脚本读了哪些 env。"""
    found: dict[str, list[str]] = {}
    for path in TS_SOURCES:
        rel = path.relative_to(REPO)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in re.findall(r"process\.env\.([A-Z0-9_]+)", line):
                found.setdefault(name, []).append(f"{rel}:{number}")
    return found


def role_references() -> dict[str, list[str]]:
    """MCP 消费者请求的角色名 -> 出现位置。"""
    found: dict[str, list[str]] = {}
    call = re.compile(r"\b(?:callRole|safeCallRole|checkRole)\(\s*['\"]([^'\"]+)['\"]")
    for path in TS_SOURCES:
        rel = path.relative_to(REPO)
        source = path.read_text(encoding="utf-8")
        for match in call.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            found.setdefault(match.group(1), []).append(f"{rel}:{line}")
    return found


def main() -> int:
    profiles, envs, roles = declared()
    models = model_references()
    env_names = env_references()
    role_names = role_references()

    unresolved: dict[str, list[str]] = {}
    for name, places in models.items():
        if name not in profiles:
            unresolved[f"model:{name}"] = places
    for name, places in env_names.items():
        if name not in envs:
            unresolved[f"env:{name}"] = places
    for name, places in role_names.items():
        if name not in roles:
            unresolved[f"role:{name}"] = places

    added = sorted(set(unresolved) - EXPECTED_UNRESOLVED)
    stale = sorted(EXPECTED_UNRESOLVED - set(unresolved))

    print(f"调用点引用：模型 {len(models)} 个、role {len(role_names)} 个、env {len(env_names)} 个")
    print(f"配置声明：profile {len(profiles)} 个、role {len(roles)} 个、env {len(envs)} 个")
    print(f"尚未覆盖：{len(unresolved)} 项（预期 {len(EXPECTED_UNRESOLVED)} 项）")

    if added:
        print("\n新增了未覆盖的引用，请先在 config/providers.example.json 里加上：")
        for key in added:
            print(f"  {key}")
            for place in unresolved[key]:
                print(f"      {place}")
    if stale:
        print("\n这些已经被配置覆盖了，请从 EXPECTED_UNRESOLVED 里删掉：")
        for key in stale:
            print(f"  {key}")

    if added or stale:
        return 1
    print("\n没有新增未覆盖项。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
