#!/usr/bin/env python3
"""Check that every role in a providers config can actually be served.

Reads config/providers.example.json (or a local copy) and reports, per role, which
provider profile would serve it and why the earlier candidates were skipped. Nothing
here changes how a run behaves; it only answers whether a run would work.

Three levels, cheapest first:

    preflight.py                  config and credentials only, no network
    preflight.py --live           one minimal real request per reachable profile
    preflight.py --live --tools   also replay a two-turn tool-use exchange

The last one exists because request-shape problems can be invisible until the second
turn: an endpoint that answers a single-turn probe correctly may still reject the
assistant content layout that a tool-using agent produces from turn two onward.

Exit code is 0 when every required role satisfies its model-count and independence contract.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import providers  # noqa: E402  路径插入之后才可导入
from api_retry import retry_http  # noqa: E402
import proxy_contract  # noqa: E402
import roles as role_models  # noqa: E402
DEFAULT_CONFIG = REPO_ROOT / "config" / "providers.example.json"
LOCAL_CONFIG = REPO_ROOT / "config" / "providers.local.json"

PROBE_PROMPT = "Reply with exactly: OK"
PROBE_MAX_TOKENS = 16
REQUEST_TIMEOUT = 60

# 命令行可以改。同一个值也要传给 OAuth token 刷新：google-auth 默认等 120 秒，
# 只调 httpx 的超时，Vertex 那两条路仍然会先卡在刷新上。
_timeout_override = None


def request_timeout() -> float:
    return REQUEST_TIMEOUT if _timeout_override is None else _timeout_override

OK = "ok"
SKIP = "skip"
FAIL = "fail"

MARK = {OK: "ok  ", SKIP: "skip", FAIL: "FAIL"}


class ProbeResult:
    """Outcome of evaluating one profile for one role."""

    def __init__(self, status: str, detail: str, elapsed: float = 0.0, quirks: list[str] | None = None,
                 stage: str = "config"):
        self.status = status
        self.detail = detail
        self.elapsed = elapsed
        self.quirks = quirks or []
        # Where this outcome was decided: "config" means no request left the process.
        # Inferring it from whether --live was passed reported a protocol mismatch,
        # rejected before any network call, as a reachable endpoint refusing us.
        self.stage = stage

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "elapsed_seconds": round(self.elapsed, 2),
            "observed_quirks": self.quirks,
            "decided_at": self.stage,
        }


def merge_with_defaults(local: dict[str, Any], defaults: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Lay a local config over the tracked defaults, per role and per profile.

    An install copies providers.example.json once and then edits it. Reading that
    copy wholesale means a role added to the tracked file later never reaches any
    existing install: `--role run_monitor` answered "unknown role" on every machine
    that had already copied. Nothing announced the gap, because from the tool's
    point of view the role simply did not exist.

    Merging is per key, not deep: a role or profile the operator has defined stays
    exactly as written, because its candidate list is their choice. Only entries
    they have never seen are filled in. Returns what was added so the run can say
    so rather than change behaviour quietly.
    """
    merged = dict(defaults)
    merged.update({k: v for k, v in local.items() if k not in ("roles", "profiles")})
    added = []
    for section in ("roles", "profiles"):
        combined = dict(defaults.get(section, {}))
        combined.update(local.get(section, {}))
        added += [f"{section[:-1]} {name}" for name in combined
                  if name not in local.get(section, {}) and not name.startswith("_")]
        merged[section] = combined
    return merged, added


def load_config(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(
            f"Config not found: {path}\n"
            f"Checked: {path}\n"
            f"Next step: copy {DEFAULT_CONFIG.relative_to(REPO_ROOT)} to "
            f"{LOCAL_CONFIG.relative_to(REPO_ROOT)} and fill in your providers."
        )
    except json.JSONDecodeError as exc:
        sys.exit(f"Config is not valid JSON: {path}\n  {exc}")


# 读 profile 上的值用运行时那一份实现。检查器和运行时在同一个问题上各写一份，答案就会
# 分叉，而这个仓已经为此付过一次代价（#135、#142）。「配没配」同理，见 evaluate()。
resolve = providers.resolve


CLI_SETTINGS = REPO_ROOT / "ar-runtime" / ".claude" / "settings.local.json"


def runtime_env(name: str) -> str | None:
    """Read a runtime variable from the process, then the generated settings env block.

    An operator who selected Gemini through /login has it in settings.local.json,
    not in the shell, so looking only at os.environ would call an enabled provider
    disabled.
    """
    value = os.environ.get(name)
    if value:
        return value
    try:
        block = json.loads(CLI_SETTINGS.read_text(encoding="utf-8")).get("env", {})
    except Exception:
        return None
    value = block.get(name)
    return value if isinstance(value, str) and value else None


def is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def role_is_active(role: dict[str, Any]) -> bool:
    """Whether an otherwise-optional role is switched on right now.

    An optional role that has been activated must block when its provider is unusable.
    Treating optional as a fixed property once let an enabled route print both
    "no usable candidate" and "all required roles resolved".
    """
    for name in role.get("_required_when_env_truthy", []):
        if is_truthy(runtime_env(name)):
            return True
    for name, wanted in (role.get("_required_when_env_equals") or {}).items():
        if (runtime_env(name) or "") == wanted:
            return True
    return False


def model_identity(profile: dict[str, Any]) -> str:
    """What makes a panel member independent: the model, not where it is hosted.

    Serving one model from two endpoints does not produce a second opinion, so the
    endpoint is deliberately not part of this. A profile that genuinely wants to be
    counted separately can say so with distinct_model.
    """
    return providers.profile_identity(profile)


def decode_json(response, elapsed: float):
    """Return the parsed body, or a ProbeResult describing why it could not be read.

    A gateway that answers 200 with an HTML error page used to raise
    JSONDecodeError straight out of the probe, ending the run. The endpoint did
    answer, so this is a request-stage failure like any other bad body.
    """
    try:
        return response.json(), None
    except Exception as exc:
        preview = response.text[:120].replace("\n", " ")
        return None, ProbeResult(
            FAIL,
            f"HTTP 200 but the body is not JSON ({type(exc).__name__}): {preview!r}",
            elapsed,
            stage="request",
        )


def model_for_role(role_name: str, profile: dict[str, Any]) -> str:
    """这个角色这次实际用哪个模型（单席位）。

    配置里的 model 是经过实测的推荐值，并非强制要求。
    AR_MODEL_<ROLE> 换掉它，运行时读的是同一个函数，所以报告和实际不会分叉。

    面板角色不能用这个：它只回答「一个席位用什么」。多席位见 with_model 与主循环里的
    席位分配，那里一个名字只落一个席位。
    """
    recommended = str(resolve(profile, "model") or "")
    return role_models.models_for(role_name, recommended)[0] if recommended \
        else (role_models.override_for(role_name) or [""])[0]


def profile_for_model(profiles: dict[str, dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Resolve a runtime model name to the same route that ``call_model`` uses."""
    if name in profiles:
        return profiles[name]
    return next(
        (profile for profile in profiles.values() if resolve(profile, "model") == name),
        None,
    )


def first_hop_of(model: str) -> str | None:
    """运行时发这个模型时，第一个真的会被发出去的 preset。"""
    try:
        import llm_client
        return llm_client.first_hop(model)
    except Exception:
        return None


def unroutable_models(names: list[str]) -> list[str]:
    """这些名字里，运行时调不了的那些。

    preflight 探的是 providers 配置里的端点，而 call_model 走的是 config 里的 preset。
    只查前者就会给一个运行时会抛 UnknownModel 的名字开绿灯：检查器绿，跑到这一步异常。
    llm_client 导不进来（缺依赖或缺配置）时不做这项检查，不能因为查不了就判红。
    """
    try:
        import llm_client
        presets = llm_client.load_legacy_config().get("presets", {})
        known = (set(llm_client._provider_models()) | set(presets)
                 | {c.get("model") for c in presets.values()} | set(llm_client._LEGACY))
    except Exception:
        return []
    return [n for n in names if n not in known]


def proxy_contract_of(profile: dict[str, Any]) -> str:
    """这个 profile 的消费者读哪套代理变量。

    写在 config 里，因为只有配置知道每个 profile 对应哪段消费者代码。声明本身不构成
    证据见 tests/test_proxy_e2e.py：它用假代理同时驱动 preflight 和真实消费者，断言两者
    连到同一地址，声明写错那条测试会红。
    """
    declared = profile.get("proxy_contract")
    if declared:
        return declared
    # 没声明时按消费者推。
    #
    # Retired Vertex-shaped profiles may still appear in migration or diagnostic
    # input. Preserve their standard proxy-variable contract without claiming a
    # current runtime consumer.
    if str(profile.get("auth", "")).startswith("vertex"):
        return proxy_contract.STANDARD_ENV
    # Keep retired preset inference aligned with the real compatibility caller.
    base = str(resolve(profile, "base_url") or "")
    return proxy_contract.contract_for_legacy_base_url(base)


def probe_client(profile: dict[str, Any], target_url: str) -> httpx.Client:
    """按 profile 的契约建 client，而不是一律直连。"""
    contract = proxy_contract_of(profile)
    kwargs = proxy_contract.effective_kwargs(
        contract, target_url, timeout=request_timeout())
    # None 只会在 proxy_unreachable 已经拦下的情形出现；走到这里说明它没拦，
    # 保守起见退回直连而不是抛 TypeError。
    return httpx.Client(**(kwargs or {"timeout": request_timeout(), "trust_env": False}))


def proxy_unreachable(profile: dict[str, Any], target_url: str, started: float):
    """契约要求走代理、而代理端口不通时，别去等满超时。

    仍然记 transport：请求已经具备构造条件，是网络路径不通。记成 config 会把刚分清的
    「缺配置」和「到不了」重新搅在一起。
    """
    contract = proxy_contract_of(profile)
    if proxy_contract.effective_kwargs(contract, target_url, timeout=1) is not None:
        # 要么不需要代理、要么代理活着、要么这台机器根本没配代理（那时消费者也直连）。
        return None
    proxy = proxy_contract.proxy_url_for(contract, target_url)
    return ProbeResult(
        FAIL,
        f"指定的代理 {proxy} 端口不通（契约 {contract}），消费者也会走它，所以这条路现在不可用",
        time.time() - started,
        stage="transport",
    )


def transport_failure(exc: Exception, started: float) -> ProbeResult:
    """A failure with no response behind it.

    Marked "transport" rather than "request" so the summary does not tell the
    operator the endpoint answered and refused. A name that will not resolve, a
    port that refuses the connection and a read that times out all land here, and
    none of them says anything about the credentials or the payload.
    """
    return ProbeResult(
        FAIL,
        f"{type(exc).__name__}: {exc}",
        time.time() - started,
        stage="transport",
    )


def _as_probe_result(reply, profile: dict[str, Any]) -> ProbeResult:
    """providers.Reply 翻译成 preflight 的 ProbeResult。

    outcome 比 ProbeResult 的三档细，所以这里会丢掉「怎么办」这一维，
    preflight 只需要「行不行」。细的那份留在 Reply 里给运行时的 retry policy 用。
    """
    if reply.ok:
        return ProbeResult(OK, f"returned {reply.text.strip()[:40]!r}",
                           reply.elapsed, stage="request")
    stage = "transport" if reply.outcome == providers.UNREACHABLE else "request"
    detail = reply.detail
    if reply.outcome == providers.REJECTED and reply.http_status:
        detail += describe_http_hint(reply, profile)
    return ProbeResult(FAIL, detail, reply.elapsed, stage=stage)


def probe_openai_chat(profile: dict[str, Any]) -> ProbeResult:
    reply = providers.dispatch_profile(
        profile,
        alias=str(resolve(profile, "model") or ""),
        timeout=request_timeout(),
        contract=proxy_contract_of(profile),
        max_tokens=PROBE_MAX_TOKENS,
    )
    return _as_probe_result(reply, profile)


def probe_anthropic_messages(profile: dict[str, Any]) -> ProbeResult:
    reply = providers.dispatch_profile(
        profile,
        alias=str(resolve(profile, "model") or ""),
        timeout=request_timeout(),
        contract=proxy_contract_of(profile),
        max_tokens=PROBE_MAX_TOKENS,
    )
    return _as_probe_result(reply, profile)


def anthropic_headers(profile: dict[str, Any]) -> dict[str, str]:
    token = resolve(profile, "auth_token") or ""
    return {
        "Content-Type": "application/json",
        # Gateways differ on which header they accept; sending both is harmless and
        # avoids a spurious auth failure on whichever one the endpoint ignores.
        "x-api-key": token,
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        # Some gateways sit behind anti-bot rules that reject default client agents.
        "User-Agent": "autoresearch-preflight/1.0",
    }


def describe_http_hint(reply, profile: dict[str, Any]) -> str:
    """把一次失败翻译成「下一步做什么」。

    这些提示挂在 Reply 上，所以检查器和运行时用的是同一份。
    """
    lowered = (reply.detail or "").lower()
    status = reply.http_status
    if "max_tokens" in lowered and "max_completion_tokens" in lowered:
        return " -> 把这个 profile 的 dialect.token_param 改成 max_completion_tokens"
    if "temperature" in lowered:
        return " -> 把这个 profile 的 dialect.supports_temperature 设成 false"
    if "not allowed to use model" in lowered:
        return " -> 这个 token 没有该模型的授权，换一个它能用的"
    if status in (401, 403):
        return " -> 检查这个 profile 的凭据环境变量"
    if status == 502:
        return " -> 上游拒绝了请求形状，加 --tools 缩小范围"
    return ""


def probe_tool_use_ordering(profile: dict[str, Any]) -> ProbeResult:
    """Replay the assistant content layouts a tool-using agent produces.

    An agent that calls a tool and keeps talking emits [text, tool_use, text] on the
    next turn. Some Anthropic-compatible gateways only accept text blocks before the
    tool_use. This sends both layouts and reports the difference, which is invisible
    to a single-turn probe.
    """
    base = (resolve(profile, "base_url") or "").rstrip("/")
    headers = anthropic_headers(profile)
    text_block = {"type": "text", "text": "Let me check that."}
    tool_block = {
        "type": "tool_use",
        "id": "toolu_preflight_0001",
        "name": "noop",
        "input": {},
    }
    tools = [{"name": "noop", "description": "does nothing", "input_schema": {"type": "object", "properties": {}}}]

    def send(assistant_content: list[dict[str, Any]]) -> int | str:
        payload = {
            "model": resolve(profile, "model"),
            "max_tokens": PROBE_MAX_TOKENS,
            "tools": tools,
            "messages": [
                {"role": "user", "content": "Use the noop tool."},
                {"role": "assistant", "content": assistant_content},
                {"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_preflight_0001",
                    "content": "done",
                }]},
            ],
        }
        try:
            url = f"{base}/v1/messages"
            with probe_client(profile, url) as client:
                return retry_http(
                    lambda: client.post(url, headers=headers, json=payload),
                    operation_name="preflight tool-use probe",
                ).status_code
        except Exception as exc:
            return f"{type(exc).__name__}"

    started = time.time()
    text_first = send([text_block, tool_block])
    trailing_text = send([text_block, tool_block, dict(text_block)])
    elapsed = time.time() - started

    if text_first != 200:
        return ProbeResult(
            FAIL,
            f"even the accepted layout [text, tool_use] returned {text_first}; "
            "tool-using agents cannot run against this endpoint",
            elapsed,
        )
    if trailing_text != 200:
        # Nothing in this repository rewrites that layout today, so an agent using
        # this endpoint fails from its second tool call onward. Reporting it while
        # still selecting the candidate would be a green run for a setup that cannot
        # work; treat it as a failure until a normaliser exists.
        return ProbeResult(
            FAIL,
            f"[text, tool_use] ok, but [text, tool_use, text] returned {trailing_text}. "
            "Agents that keep talking after a tool call fail from the second turn, and "
            "nothing here rewrites the layout. Put a normalising proxy in front of this "
            "endpoint, or use one that accepts the layout.",
            elapsed,
            quirks=["text_before_tool_use"],
        )
    return ProbeResult(OK, "both assistant content layouts accepted", elapsed, stage="request")


PROBES = {
    "openai_chat": probe_openai_chat,
    "anthropic_messages": probe_anthropic_messages,
}


def evaluate(
    profile_name: str,
    profile: dict[str, Any],
    live: bool,
    tools: bool,
    required_api: str | list | None = None,
    cache: dict[str, ProbeResult] | None = None,
) -> ProbeResult:
    api = profile.get("api")
    if api not in PROBES:
        return ProbeResult(FAIL, f"unknown api {api!r}; expected one of {', '.join(sorted(PROBES))}")

    # Some roles can only speak certain protocols. Claude Code's main loop talks
    # Anthropic Messages by default, so pointing it at an OpenAI-compatible profile
    # passes every credential check and then cannot run -- unless the consumer has a
    # compatibility layer, which is why this accepts a list.
    allowed = [required_api] if isinstance(required_api, str) else required_api
    if allowed and api not in allowed:
        return ProbeResult(
            FAIL,
            f"this role requires api={' or '.join(allowed)} but the profile is {api}",
            stage="config",
        )

    # 「配没配」问 providers，投影问的是同一个函数。这里原来自己判一遍凭据、再自己判
    # 一遍开关，两份实现会各自演化，而 #135 就是两侧选出不同候选的结果。
    #
    # 开关和凭据一起报：一个 profile 可能既缺 key 又没打开关，先返回其中一个会让操作者
    # 补完一样再跑一次才看到另一样。
    missing = providers.unmet_requirements(profile)
    if missing:
        return ProbeResult(SKIP, f"needs {', '.join(missing)}")

    if not live:
        return ProbeResult(OK, "configured (no request sent; add --live to verify)")

    # Roles share profiles; probing per role would bill the same endpoint repeatedly.
    if cache is not None and profile_name in cache:
        cached = cache[profile_name]
        return ProbeResult(
            cached.status, f"{cached.detail} (reused)", 0.0, cached.quirks,
            # Carry the stage. Dropping it sent every reused failure to the
            # default, so the second role sharing a profile was told the endpoint
            # answered when the first had never reached it.
            stage=cached.stage,
        )

    # Each probe records its own stage: whether it ever reached the endpoint is
    # something only it knows. Stamping "request" here reported a connection that
    # was never established as an endpoint that answered and refused.
    result = PROBES[api](profile)
    if result.status == OK and tools and api == "anthropic_messages":
        ordering = probe_tool_use_ordering(profile)
        result.detail = f"{result.detail}; {ordering.detail}"
        result.quirks = ordering.quirks
        if ordering.status == FAIL:
            result.status = FAIL
    if cache is not None:
        cache[profile_name] = result
    return result


def role_check_order(roles: dict[str, dict[str, Any]], requested: list[str] | None) -> list[str]:
    """Order requested roles after the roles their independence depends on."""
    selected = set(requested or roles)
    pending = list(selected)
    while pending:
        role_name = pending.pop()
        dependency = role_models.independent_of(role_name, roles.get(role_name))
        if not dependency:
            continue
        if dependency not in roles:
            raise ValueError(f"{role_name} requires missing role {dependency}")
        if dependency not in selected:
            selected.add(dependency)
            pending.append(dependency)

    ordered: list[str] = []
    visiting: set[str] = set()

    def add_role(role_name: str) -> None:
        if role_name in ordered:
            return
        if role_name in visiting:
            raise ValueError(f"cyclic role independence involving {role_name}")
        visiting.add(role_name)
        dependency = role_models.independent_of(role_name, roles.get(role_name))
        if dependency in selected:
            add_role(dependency)
        visiting.remove(role_name)
        ordered.append(role_name)

    for role_name in roles:
        if role_name in selected:
            add_role(role_name)
    return ordered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=None,
                        help="providers config; defaults to the local copy if present")
    parser.add_argument("--live", action="store_true", help="send one minimal real request per profile")
    parser.add_argument("--tools", action="store_true", help="with --live, also replay a two-turn tool-use exchange")
    parser.add_argument("--role", action="append", help="check only this role; repeatable")
    parser.add_argument("--json", type=Path, help="also write the report as JSON")
    parser.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                        help=f"per-request timeout, including the OAuth token refresh "
                             f"(default {REQUEST_TIMEOUT}s)")
    args = parser.parse_args()

    global _timeout_override
    _timeout_override = args.timeout

    # Resolve the effective configuration once so preflight and projection use the same routes.
    selected = args.config or os.environ.get("AUTORESEARCH_CONFIG")
    explicit = Path(selected).expanduser() if selected else None
    if explicit is not None and not explicit.exists():
        load_config(explicit)  # 它给的是可执行的报错，不是一句 traceback
    config, config_path, added = providers.load_effective_config(REPO_ROOT, explicit)
    if explicit is not None:
        os.environ["AUTORESEARCH_CONFIG"] = str(config_path)
    # 走读取器而不是直接摸 `profiles` / `candidates`：v2 里它们叫 models 和 routes，
    # 直接读键在 v2 上不会报错，会安静地拿到空字典，每个角色都变成「没有可用候选」，
    # 而这跟真的没配好长得一模一样。
    profiles = providers.as_profiles(config)
    roles = config.get("roles", {})
    if added:
        print(f"{config_path.name} does not define {', '.join(sorted(added))}; "
              f"taking them from {DEFAULT_CONFIG.name}.\n")
    if args.tools and not args.live:
        parser.error("--tools has no effect without --live")

    # A misspelt role name would otherwise filter out every role and exit 0.
    if args.role:
        unknown = [name for name in args.role if name not in roles]
        if unknown:
            parser.error(
                f"unknown role(s): {', '.join(unknown)}. "
                f"Known roles: {', '.join(roles) or '(none in this config)'}"
            )
    try:
        checked_roles = role_check_order(roles, args.role)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"config : {config_path}")
    mode = "configuration only"
    if args.live:
        mode = "live requests" + (" + tool-use replay" if args.tools else "")
    print(f"mode   : {mode}")
    print()

    report: dict[str, Any] = {"config": str(config_path), "live": args.live, "roles": {}}
    unresolved = []
    probe_cache: dict[str, ProbeResult] = {}
    selected_identities: dict[str, set[str]] = {}

    for role_name in checked_roles:
        role = roles[role_name]
        configured_candidates = providers.role_candidates(config, role_name)
        override = role_models.override_for(role_name)
        candidates = override or configured_candidates
        all_used = role.get("_all_candidates_used", False)
        # Optional is a default, not a property. A role switched on in the
        # environment has to be able to fail, and naming one on the command line
        # is a question about that role -- answering "all required roles resolved"
        # to `--role ccb_gemini` after printing "no usable candidate" is not an
        # answer.
        asked_for = bool(args.role) and role_name in args.role
        activated = role_is_active(role)
        optional = role_models.optional_by_default(role_name, role) and not activated and not asked_for
        if role.get("_optional", False) and not role_models.optional_by_default(role_name, role):
            print(f"{role_name}  (treated as required by the current workflow contract)")
        elif role.get("_optional", False) and (activated or asked_for):
            why = "it is switched on in this environment" if activated else "you asked for it by name"
            print(f"{role_name}  (normally optional; treated as required because {why})")
        required_api = role.get("_requires_api")
        # 硬门槛由共享角色策略兜底，配置只能提高、不能降低。这样旧 local JSON 仍写着
        # `_recommended_min_available: 2` 时，运行时和 preflight 也不会重新放行单模型面板。
        min_available = role_models.minimum_available(role_name, role)
        recommended_min = role_models.recommended_available(role_name, role)
        independent_from = role_models.independent_of(role_name, role)
        excluded_identities = selected_identities.get(independent_from or "", set())

        # 推荐值就是候选们各自的 model。面板角色会真的把候选全跑一遍，所以它的推荐是
        # 这里读的是一组模型，键名必须和配置里的拼写一致
        #（`_recommended_min_available` / `_all_candidates_used`），拼错的话这个分支永远
        # 不执行，五席位角色只打印一个名字。
        names = [n for n in (str(resolve(profiles.get(c) or {}, "model") or "")
                             for c in configured_candidates) if n]
        recommended = names[:max(min_available, recommended_min)] \
            if all_used else (names[0] if names else "")
        print(f"{role_name}  {role.get('_purpose', '')}")
        if recommended or override:
            print(f"    {role_models.describe(role_name, recommended)}")
        if role.get("_needs"):
            print(f"    这个角色要什么：{role['_needs']}")
        for name in (override or ([recommended] if isinstance(recommended, str)
                                  else list(recommended or []))):
            hop = first_hop_of(name)
            if hop and hop != name:
                print(f"    注意：{name} 的第一跳是 preset「{hop}」，"
                      "下面探的就是它；有回落梯子时，探最后一跳等于没探（#52）")

        if override:
            unroutable = unroutable_models(override)
            if unroutable:
                # 探得通不等于跑得通：这些名字 call_model 会直接抛 UnknownModel。
                print(f"    => 运行时调不了：{', '.join(unroutable)}"
                      f"（{role_models.env_name(role_name)}）")
                print("       把它加进 config/providers.local.json 的 models，或换一个已配置的代称")
                if not optional:
                    unresolved.append((role_name, {n: ("fail", "config") for n in unroutable}))
                print()
                continue

        chosen: list[str] = []
        seen_identities: set[str] = set()
        used_model: dict[str, str] = {}
        collapsed = 0
        # 覆盖会完整替换角色候选，运行时也是如此。模型代称自带 route；把覆盖名钉到原候选的
        # endpoint 上会让 preflight 和 call_model 实际调用不同服务。
        for candidate in candidates:
            profile = profile_for_model(profiles, candidate)
            if profile is None:
                result = ProbeResult(FAIL, "no profile with this name")
            else:
                if args.live:
                    # 探测前就把名字和这条路怎么走打出来，并 flush。一次探测最坏等满
                    # 超时，期间零输出是「看起来卡死」的另一半原因；管道下 stdout 是
                    # 块缓冲，不 flush 连开头两行都要等缓冲区满。
                    route = proxy_contract.describe(
                        proxy_contract_of(profile), str(resolve(profile, "base_url") or ""))
                    print(f"    …  {candidate:<26} {route} 探测中", end="", flush=True)
                result = evaluate(
                    candidate, profile, args.live, args.tools,
                    required_api=required_api, cache=probe_cache,
                )

            suffix = f" [{result.elapsed:.1f}s]" if result.elapsed else ""
            print(f"\r    {MARK[result.status]} {candidate:<26} {result.detail}{suffix}")
            report["roles"].setdefault(role_name, {})[candidate] = result.as_dict()

            if result.status == OK:
                # A panel needs independent models. Two candidates naming the same
                # endpoint and model are one model, however they are spelled, and
                # counting them twice would let a one-model panel look like two.
                # 身份取覆盖**后**的 profile：取覆盖前的话，三个名字指向同一个模型仍会
                # 被算成三票，去重形同虚设。
                identity = model_identity(profile) if profile else candidate.strip().lower()
                if identity in excluded_identities:
                    collapsed += 1
                    print(f"         (same model as {independent_from}; cannot satisfy independence)")
                elif identity in seen_identities:
                    collapsed += 1
                    print("         (same model as an earlier candidate; not counted twice)")
                else:
                    seen_identities.add(identity)
                    chosen.append(candidate)
                    used_model[candidate] = str(resolve(profile, "model") or "") if profile else ""
                if not all_used:
                    if chosen:
                        break

        selected_identities[role_name] = seen_identities

        enough = len(chosen) >= min_available
        if chosen and enough:
            if all_used:
                shown = ', '.join(f"{c}（{used_model.get(c, '')}）" for c in chosen)
                print(f"    => uses all of: {shown}")
                # 少掉的席位分两种，混在一起报会让人去补凭证解决一个其实是重复的问题：
                # 一种是候选不可用（缺凭证或探测失败），一种是候选可用但和前面撞了同一个
                # 模型，票数上不算第二票。
                missing = len(candidates) - len(chosen) - collapsed
                if missing or collapsed:
                    why = []
                    if missing:
                        why.append(f"{missing} 个候选不可用")
                    if collapsed:
                        why.append(f"{collapsed} 个与前面是同一个模型")
                    print(f"       {'，'.join(why)}；cross-review runs with fewer "
                          "independent models")
                if len(chosen) < recommended_min:
                    print(f"       建议使用 {recommended_min} 个独立模型；当前为 {len(chosen)} 个。"
                          "已满足硬门槛，但面板规模低于建议值")
            else:
                used = used_model.get(chosen[0], "")
                print(f"    => {chosen[0]}" + (f"（{used}）" if used else ""))
                if chosen[0] != candidates[0]:
                    # 用到后面的候选不是降级。推荐值的意思是「我们实测过这个角色能用它」，
                    # 不是要求。写成 fell back 会让人以为出了问题，跑去补一个其实不需要
                    # 的 provider。上手成本就是这么堆起来的。
                    top = str(resolve(profiles.get(candidates[0]) or {}, "model") or "")
                    print(f"       推荐的 {top or candidates[0]} 这次没配；这个角色不挑模型，"
                          f"换成别的照样跑（要指定就设 {role_models.env_name(role_name)}）")
        elif chosen:
            print(f"    => only {len(chosen)} of {min_available} required models available: {', '.join(chosen)}")
            print(f"       this role needs at least {min_available}; with fewer it cannot reach its own threshold")
            if not optional:
                statuses = {
                    c: (
                        report["roles"].get(role_name, {}).get(c, {}).get("status"),
                        report["roles"].get(role_name, {}).get(c, {}).get("decided_at"),
                    )
                    for c in candidates
                }
                unresolved.append((role_name, statuses))
        else:
            if independent_from and excluded_identities:
                print(f"    => no model independent of {independent_from}")
            else:
                print("    => no usable candidate")
            if not optional:
                statuses = {
                    c: (
                        report["roles"].get(role_name, {}).get(c, {}).get("status"),
                        report["roles"].get(role_name, {}).get(c, {}).get("decided_at"),
                    )
                    for c in candidates
                }
                unresolved.append((role_name, statuses))
        print()

    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"json report: {args.json}")

    if unresolved:
        names = ", ".join(name for name, _ in unresolved)
        print(f"{len(unresolved)} required role(s) unresolved: {names}")

        # Separate the two causes: a role nobody has credentials for needs different
        # action from one whose endpoint answered and refused.
        only_missing = [n for n, s in unresolved if all(v[0] == SKIP for v in s.values())]
        # Three different actions, so three buckets. Each probe records which one it
        # reached; nothing here guesses from whether --live was passed. Collapsing
        # transport into request told operators to change their configuration when
        # the endpoint had never answered at all.
        def failed_at(name: str, states: dict, stage: str) -> bool:
            return any(v[0] == FAIL and v[1] == stage for v in states.values())

        unreachable = [n for n, s in unresolved if failed_at(n, s, "transport")]
        refused_on_wire = [n for n, s in unresolved
                           if failed_at(n, s, "request") and n not in unreachable]
        refused_in_config = [n for n, s in unresolved
                             if failed_at(n, s, "config")
                             and n not in unreachable and n not in refused_on_wire]
        if only_missing:
            print(f"  no credentials configured: {', '.join(only_missing)}")
            print("  -> set the environment variables listed above, or add a profile you can reach")
        if unreachable:
            print(f"  never reached the endpoint: {', '.join(unreachable)}")
            print("  -> check the host, the port and whatever sits between you and it;"
                  " the credentials were never offered")
        if refused_on_wire:
            print(f"  endpoint answered and refused the request: {', '.join(refused_on_wire)}")
            print("  -> apply the fix noted next to each failure above; it is a config change, not a credential one")
        if refused_in_config:
            print(f"  rejected before any request was sent: {', '.join(refused_in_config)}")
            print("  -> a configuration mismatch, such as a role requiring a different api")
        if not args.live and only_missing and not (unreachable or refused_on_wire or refused_in_config):
            print("  note: this run did not send requests, so only credential gaps could be detected")
        return 1

    print("all required roles resolved")
    if not args.live:
        print("This checked configuration only. Add --live to confirm the endpoints actually answer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
