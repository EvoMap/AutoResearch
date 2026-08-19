"""一个请求怎么构造、怎么发、怎么读结果——只写一处。

检查器和运行时各写一遍「怎么发一个请求」，迟早会报的是两个系统的状态。这个仓库为此清过
好几轮：覆盖只接到检查器上、身份取自覆盖前的配置、门认不出重构后的新形状。所以把这三件事
收敛成 `build → send → read`，两边调同一个。

三层是独立的，别再缠在一起：

    方言   请求长什么样（字段名、URL 形状、从哪读回文本）
    认证   怎么证明身份（Bearer / x-api-key / 两个都发）
    传输   怎么送出去（走不走代理，按 proxy_contract）

`Reply.outcome` 分得比「成功 / 失败」细，因为下游要据此做不同的事：模型没话说、我们读不懂、
端点答了并拒绝、压根没连上，是四件不同的事。`REFUSED` 这类粗分类会把鉴权拒绝、参数错误、
429 和 5xx 压成同一个持久化结论，于是重试策略无从谈起。

范围：单轮文本补全。工具调用探测是 Anthropic 专用的另一件事，不进这里——不写死这条，三个月
后这个协议会有九个方法，四个 raise NotImplementedError。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

import api_retry
import proxy_contract

# 结果词汇。按 verdicts.py 的路子：判不了就是判不了，不倒向任何一边。
DELIVERED = "delivered"        # 200，形状对，有文本
EMPTY = "empty"                # 200，形状对，文本是空的——模型没话说
UNREADABLE = "unreadable"      # 200，但不是承诺的形状——我们读不懂
REJECTED = "rejected"          # 4xx，端点答了并且说不行
THROTTLED = "throttled"        # 429，限流
REMOTE_ERROR = "remote_error"  # 5xx，对面出错
UNREACHABLE = "unreachable"    # 压根没拿到响应
UNCONFIGURED = "unconfigured"  # 没出过进程
GEO_BLOCKED = "geo_blocked"    # 按来源国家被拒——固定执行位置后它意味着环境漂移

# 决定要不要重试的时候读这个，而不是读 outcome：outcome 说发生了什么，policy 说怎么办。
# GEO_BLOCKED 故意不在里面：重试只会把执行环境的漂移藏起来。
RETRIABLE = {UNREACHABLE, THROTTLED, REMOTE_ERROR}

PROBE_PROMPT = "Reply with exactly: OK"
PROBE_MAX_TOKENS = 16
DEFAULT_MODEL_TIMEOUT = 900.0


@dataclass(frozen=True)
class Wire:
    """一个请求，作为数据。还没发出去。

    `build` 造它、`send` 发它、`read` 解它，三步分开，于是 golden test 是纯函数测试，
    不用开 socket 就能断言「发出去的字节一个都没变」。
    """

    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    proxy_contract: str = proxy_contract.DIRECT
    timeout: float = DEFAULT_MODEL_TIMEOUT
    stream: bool = False

    def redacted_headers(self) -> dict[str, str]:
        """能进日志和快照的那份。凭据换成形状，不换成值。"""
        safe = {}
        for name, value in self.headers.items():
            low = name.lower()
            if low in {"authorization", "x-api-key", "x-goog-api-key"}:
                safe[name] = f"<{len(value)} chars>"
            else:
                safe[name] = value
        return safe


@dataclass
class Reply:
    """一次调用的结果，以及为什么。"""

    outcome: str
    text: str = ""
    detail: str = ""
    http_status: int | None = None
    provider_code: str = ""
    finish_reason: str = ""
    retry_after: str = ""
    elapsed: float = 0.0
    stage: str = "request"          # config | transport | request，与 ProbeResult 同词汇
    attempts: list[dict] = field(default_factory=list)
    # 输入/输出 token。验收判据要读它们，而且 EMPTY 与「预算耗尽」是不是同一件事，
    # 只有拿到 usage 才判得了。
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome == DELIVERED

    @property
    def retriable(self) -> bool:
        return self.outcome in RETRIABLE


class Dialect(Protocol):
    """一种 wire 格式。实现只做三件事：造请求、读响应、给一个探针请求。"""

    name: str

    def build(self, endpoint: dict[str, Any], model: str, prompt: str,
              params: dict[str, Any]) -> Wire: ...

    def read(self, response: httpx.Response, elapsed: float) -> Reply: ...


def _decode(response: httpx.Response, elapsed: float) -> tuple[dict | None, Reply | None]:
    try:
        return response.json(), None
    except ValueError:
        head = response.text.strip()[:120]
        return None, Reply(UNREADABLE, detail=f"HTTP 200 但不是 JSON: {head!r}",
                           http_status=response.status_code, elapsed=elapsed)


def _non_200(response: httpx.Response, elapsed: float) -> Reply:
    """非 200 分三类，因为要做的事不同：限流等一会、对面出错可重试、其余是我们的问题。"""
    code, detail = "", response.text.strip()[:200]
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "")
            detail = str(error.get("message") or detail)[:200]
    except ValueError:
        pass

    if response.status_code == 429:
        outcome = THROTTLED
    elif response.status_code >= 500:
        outcome = REMOTE_ERROR
    else:
        outcome = REJECTED
    return Reply(outcome, detail=f"HTTP {response.status_code}: {detail}",
                 http_status=response.status_code, provider_code=code,
                 retry_after=response.headers.get("retry-after", ""), elapsed=elapsed)


class OpenAIChat:
    """OpenAI Chat Completions。Azure 的兼容形态、各类聚合网关、自建 vLLM 都是它。"""

    name = "openai_chat"

    def url(self, base: str, style: str = "mcp") -> str:
        """Build the final request URL using the resolved route contract."""
        trimmed = base.rstrip("/")
        if trimmed.endswith("/chat/completions"):
            return trimmed
        if style == "append":
            return trimmed + "/chat/completions"
        if style == "mcp":
            if trimmed.endswith("/v1"):
                return trimmed + "/chat/completions"
            return trimmed + "/v1/chat/completions"
        raise ValueError(f"unsupported OpenAI chat URL style: {style!r}")

    def build(self, endpoint, model, prompt, params) -> Wire:
        token_param = params.get("token_param", "max_tokens")
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            token_param: params.get("max_tokens", PROBE_MAX_TOKENS),
        }
        # 有的部署只接受默认 temperature，带上就 400。这是「端点 × 模型」这条绑定的属性，
        # 不是厂商的属性：同一个 Azure 端点上，一个模型收它、另一个拒它。
        if params.get("supports_temperature", True):
            body["temperature"] = params.get("temperature", 0.3)
        if "reasoning_effort" in params:
            reasoning_effort = params["reasoning_effort"]
            allowed = {"none", "low", "medium", "high"}
            if reasoning_effort not in allowed:
                raise ValueError(
                    "reasoning_effort must be one of: none, low, medium, high"
                )
            body["reasoning_effort"] = reasoning_effort
        return Wire(
            method="POST", url=self.url(endpoint["base_url"], params.get("url_style", "mcp")),
            body=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {endpoint['api_key']}"},
            proxy_contract=endpoint.get("proxy_contract", proxy_contract.DIRECT),
            timeout=endpoint.get("timeout", DEFAULT_MODEL_TIMEOUT),
        )

    def read(self, response, elapsed) -> Reply:
        if response.status_code != 200:
            return _non_200(response, elapsed)
        body, failure = _decode(response, elapsed)
        if failure:
            return failure
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return Reply(UNREADABLE, detail="200 但没有 choices 数组",
                         http_status=200, elapsed=elapsed)
        first = choices[0] if isinstance(choices[0], dict) else {}
        finish = str(first.get("finish_reason") or "")
        text = (first.get("message") or {}).get("content")
        if not text:
            # 推理模型先把预算花在隐式思考上，这时 200 是真的、文本是真的空。
            return Reply(EMPTY, detail="200 但没有文本。推理模型会先耗预算做隐式思考，"
                                       "把 token 预算调大再试",
                         http_status=200, finish_reason=finish, elapsed=elapsed)
        return Reply(DELIVERED, text=str(text), http_status=200,
                     finish_reason=finish, elapsed=elapsed)


class AnthropicMessages:
    """Anthropic Messages。官方端点和各类兼容网关。"""

    name = "anthropic_messages"

    def build(self, endpoint, model, prompt, params) -> Wire:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": endpoint.get("anthropic_version", "2023-06-01"),
        }
        token = endpoint.get("auth_token") or endpoint.get("api_key") or ""
        # 两个头一起发：官方认 x-api-key，部分兼容网关只认 Bearer。少发一个就有一类端点
        # 会被误判成鉴权失败。
        headers["x-api-key"] = token
        headers["Authorization"] = f"Bearer {token}"
        if endpoint.get("user_agent"):
            headers["User-Agent"] = endpoint["user_agent"]
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": params.get("max_tokens", PROBE_MAX_TOKENS),
            "messages": [{"role": "user", "content": prompt}],
        }
        if params.get("supports_temperature", True):
            body["temperature"] = params.get("temperature", 0.3)
        return Wire(
            method="POST", url=endpoint["base_url"].rstrip("/") + "/v1/messages",
            headers=headers, body=body,
            proxy_contract=endpoint.get("proxy_contract", proxy_contract.DIRECT),
            timeout=endpoint.get("timeout", DEFAULT_MODEL_TIMEOUT),
        )

    def read(self, response, elapsed) -> Reply:
        if response.status_code != 200:
            return _non_200(response, elapsed)
        body, failure = _decode(response, elapsed)
        if failure:
            return failure
        blocks = body.get("content")
        if not isinstance(blocks, list):
            return Reply(UNREADABLE, detail="200 但 content 不是数组",
                         http_status=200, elapsed=elapsed)
        finish = str(body.get("stop_reason") or "")
        text = "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")
        if not text:
            # 只有 thinking / tool_use 块时，200 是真的、文本是真的空。原来这里返回 None，
            # 和「网络挂了」长得一模一样。
            kinds = sorted({b.get("type") for b in blocks if isinstance(b, dict)})
            return Reply(EMPTY, detail=f"200 但没有 text 块，只有 {kinds}",
                         http_status=200, finish_reason=finish, elapsed=elapsed)
        return Reply(DELIVERED, text=text, http_status=200,
                     finish_reason=finish, elapsed=elapsed)


DIALECTS: dict[str, Dialect] = {
    "openai_chat": OpenAIChat(),
    "anthropic_messages": AnthropicMessages(),
}


def send(wire: Wire, dialect: Dialect) -> Reply:
    """把请求发出去并按方言解结果。全仓只有这一个地方发 HTTP。

    代理走 proxy_contract 的共享决策点，所以检查器和运行时落在同一个出口地址上——这一点
    有 tests/test_proxy_e2e.py 用假代理端到端钉着。
    """
    started = time.time()
    kwargs = proxy_contract.effective_kwargs(
        wire.proxy_contract, wire.url, timeout=wire.timeout)
    if kwargs is None:
        return Reply(UNREACHABLE, stage="transport",
                     detail=f"{wire.proxy_contract} 契约要求的代理不可用",
                     elapsed=time.time() - started)
    try:
        with httpx.Client(**kwargs) as client:
            response = client.request(wire.method, wire.url,
                                      headers=wire.headers, json=wire.body)
    except Exception as exc:
        return Reply(UNREACHABLE, stage="transport",
                     detail=f"{type(exc).__name__}: {exc}"[:200],
                     elapsed=time.time() - started)
    return dialect.read(response, time.time() - started)


def call(dialect: Dialect, endpoint: dict[str, Any], model: str,
         prompt: str = PROBE_PROMPT, **params) -> Reply:
    """build → send → read，一次走完。检查器和运行时调的都是它。"""
    return send(dialect.build(endpoint, model, prompt, params), dialect)


# ---------------------------------------------------------------------------
# Configuration formats: v1 uses flat profiles; v2 uses endpoints, routed models, and roles.
# Both remain valid inputs so callers can combine either shape with the tracked v2 defaults.
# ---------------------------------------------------------------------------

def is_v2(config: dict[str, Any]) -> bool:
    return int(config.get("version", 1)) >= 2 or "endpoints" in config


# v1 remains a supported compatibility input; new configurations should use v2.
COMPATIBILITY_WINDOW = """v1 remains accepted through `as_profiles` and
`_merge_across_formats`; tracked examples and new configurations use v2."""


def uses_v1(config: dict[str, Any]) -> bool:
    """这份配置里还有 v1 形状在起作用吗。兼容窗口能不能关，问的就是它。"""
    return bool(config.get("profiles")) or bool(config.get("presets"))


def as_profiles(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把配置摊成 v1 形状的 profiles，供还没迁移的消费者读。

    v2 的一个 model 有多条 route，摊平时取第一条——摊平本来就是有损的，这一层只是让
    兼容窗口内的旧代码继续工作，新代码应该直接读 routes。
    """
    if not is_v2(config):
        return {k: v for k, v in config.get("profiles", {}).items()
                if isinstance(v, dict) and not k.startswith("_")}

    endpoints = config.get("endpoints", {})
    flat: dict[str, dict[str, Any]] = {}
    for name, model in config.get("models", {}).items():
        routes = model.get("routes") or []
        if not routes:
            continue
        route = routes[0]
        endpoint = endpoints.get(route.get("endpoint"), {})
        profile: dict[str, Any] = {"api": endpoint.get("dialect", "")}
        # 没有显式 identity 就不要造一个：`distinct_model` 一旦有值，面板判定就不再去
        # 解析 model_env，于是三个指向同一个模型的席位会被算成三票。
        if model.get("identity"):
            profile["distinct_model"] = model["identity"]
        for key in ("base_url", "base_url_env", "proxy_contract", "transport",
                    "retry_attempts", "retry_delay_seconds"):
            if endpoint.get(key) is not None:
                profile[key] = endpoint[key]
        if endpoint.get("url_env_fallback"):
            profile["base_url_env_fallback"] = endpoint["url_env_fallback"]
        # 凭据落在方言要的那个键上：anthropic_messages 问 auth_token，其余问 api_key。
        # 一律写成 api_key_env 的话，anthropic 那几个候选会被报成缺 <auth_token>——
        # 端点明明认得那个变量，检查器却说没配。同一端点上合并出来的其余变量进 fallback，
        # 那正是「设了其中任何一个都能用」的意思。
        creds = endpoint.get("credential_env") or []
        if creds:
            key = "auth_token" if endpoint.get("dialect") == "anthropic_messages" else "api_key"
            profile[f"{key}_env"] = creds[0]
            if len(creds) > 1:
                profile[f"{key}_env_fallback"] = creds[1:]
        if route.get("wire_name") is not None:
            profile["model"] = route["wire_name"]
        if route.get("wire_name_env") is not None:
            profile["model_env"] = route["wire_name_env"]
        if route.get("wire_name_env_fallback") is not None:
            profile["model_env_fallback"] = route["wire_name_env_fallback"]
        # Preserve any environment mode explicitly required by the downstream consumer.
        for key in ("requires_env", "_measured"):
            if route.get(key) is not None:
                profile[key] = route[key]
        if model.get("requires_env") is not None:
            profile["requires_env"] = model["requires_env"]
        if route.get("params"):
            profile["dialect"] = route["params"]
        if route.get("quirks"):
            profile["quirks"] = route["quirks"]
        flat[name] = profile
    return flat


def env_names(profile: dict[str, Any], key: str) -> list[str]:
    """这个键会去读哪几个环境变量，按优先级。

    `*_env_fallback` 是真实存在的：ar-external-critic-mcp 的 getGptConfig() 接受
    GPT_CRITIC_* 或 OPENAI_*，只看第一个会把一台配好的机器判成没配。
    """
    names: list[str] = []
    primary = profile.get(f"{key}_env")
    if primary:
        names.append(primary)
    fallback = profile.get(f"{key}_env_fallback")
    if isinstance(fallback, str):
        names.append(fallback)
    elif isinstance(fallback, list):
        names.extend(n for n in fallback if isinstance(n, str))
    return names


def resolve(profile: dict[str, Any], key: str, env: dict[str, str] | None = None) -> str | None:
    """读 profile 上的一个值，或它 `*_env` 指向的环境变量。"""
    environ = os.environ if env is None else env
    for name in env_names(profile, key):
        value = environ.get(name)
        if value:
            return value
    return profile.get(key)


def endpoint_for(profile: dict[str, Any], *, timeout: float = DEFAULT_MODEL_TIMEOUT,
                 contract: str | None = None,
                 env: dict[str, str] | None = None) -> dict[str, Any]:
    """profile → 这一层认的 endpoint。翻译只此一份。

    检查器和运行时必须发同一个请求，所以这段换算不能各写各的——两边的 UA、代理契约或
    凭据选择差一点，探得通就不代表跑得通，而那种绿是最难发现的。
    """
    return {
        "base_url": resolve(profile, "base_url", env) or "",
        "api_key": resolve(profile, "api_key", env) or "",
        "auth_token": (resolve(profile, "auth_token", env)
                       or resolve(profile, "api_key", env) or ""),
        "proxy_contract": contract or profile.get("proxy_contract") or proxy_contract.DIRECT,
        "timeout": timeout,
        # 运行时发的就是这个 UA，检查器跟着发同一个：有网关按 UA 做反爬。
        "user_agent": "curl/7.88.1",
    }


def retry_delay(reply: "Reply", attempt: int, base: float = 1.0, ceiling: float = 30.0) -> float:
    """下一次重试等多久。`Retry-After` 优先，其次指数退避。

    对面说了等多久就等多久：自己算一个更短的只会再撞一次限流，而更长的白等。头部是
    秒数或 HTTP 日期，这里只认秒数——日期形式要处理时钟偏移，没有证据说明这些端点在用它。
    """
    if reply.retry_after.strip().isdigit():
        return min(float(reply.retry_after.strip()), ceiling)
    return min(base * (2 ** attempt), ceiling)


def with_retries(send_once, *, limit: int = api_retry.DEFAULT_ATTEMPTS, sleep=None,
                 base: float = 1.0) -> "Reply":
    """把一次调用重试到成功或到头，并把每次尝试记进 `Reply.attempts`。

    `Reply.attempts` 这个字段一直存在，却没有任何东西写它：外层看到的只是最后一次的
    结果，429 撞了几次、5xx 等了多久、总共花了多少时间，全部不可见。

    只重试 `RETRIABLE` 里的那几种。`GEO_BLOCKED` 刻意不在里面——执行位置固定之后它
    意味着环境漂移，重试只会把漂移藏起来；`REJECTED` 是鉴权或参数错，重发一万次也一样。
    """
    history: list[dict] = []

    def record(attempt, reply, error):
        if error is not None:
            return
        history.append({"attempt": attempt, "outcome": reply.outcome,
                        "http_status": reply.http_status,
                        "provider_code": reply.provider_code,
                        "elapsed": round(reply.elapsed, 3)})

    reply = api_retry.retry_call(
        send_once,
        attempts=max(1, limit),
        should_retry_result=lambda result: result.outcome in RETRIABLE,
        should_retry_exception=lambda _error: False,
        delay_for=lambda attempt, result, _error: retry_delay(
            result, attempt - 1, base),
        sleep=sleep,
        on_attempt=record,
        operation_name="模型 API 调用",
    )
    reply.attempts = history
    return reply


# 一个方言要哪些东西才算配好。同一组里的键互为替代，设了任意一个就行。
#
# anthropic_messages 的凭据接受 api_key，因为 `AnthropicMessages.build` 发的就是
# `auth_token or api_key`，两个头一起带。只认 auth_token 会把一台真能跑的机器判成没配。
CREDENTIALS: dict[str, tuple[tuple[str, ...], ...]] = {
    "openai_chat": (("base_url",), ("api_key",)),
    "openai_responses": (("base_url",), ("api_key",)),
    "anthropic_messages": (("base_url",), ("auth_token", "api_key")),
    "vertex_generate": (("service_account",), ("project_id",)),
}


def missing_credentials(profile: dict[str, Any],
                        env: dict[str, str] | None = None) -> list[str]:
    """这个 profile 的凭据和 URL 还缺哪些，按消费者读的变量名列出来。

    名字是给操作者照着设的，所以一组替代键要连着报（`A or B`）：只报第一个会让人以为
    非它不可，而消费者认另一个。
    """
    api = profile.get("api")
    if api == "gemini":
        # Gemini 按它自己声明的方式鉴权：service account 那条要两个值，api key 那条要一个。
        groups = ((("service_account",), ("project_id",))
                  if profile.get("auth", "vertex") == "vertex" else (("api_key",),))
    else:
        groups = CREDENTIALS.get(api or "", ())

    missing = []
    for group in groups:
        if any(resolve(profile, key, env) for key in group):
            continue
        names = [name for key in group for name in env_names(profile, key)]
        missing.append(" or ".join(names) if names else f"<{group[0]}>")
    return missing


def missing_required_env(profile: dict[str, Any],
                         env: dict[str, str] | None = None) -> list[str]:
    """Return consumer mode variables that do not equal the profile's required values."""
    environ = os.environ if env is None else env
    required = profile.get("requires_env") or {}
    return [f"{name}={want}" for name, want in required.items()
            if str(environ.get(name, "")) != str(want)]


def unmet_requirements(profile: dict[str, Any],
                       env: dict[str, str] | None = None) -> list[str]:
    """这个 profile 现在还差什么才能用。空列表就是能用。

    「能用」只在这里定义一次：preflight 会跳过配不通的候选去试下一个，投影直接取
    `usable_candidates` 的第一个，两边各判一遍就会选出不同的候选——#135 正是这么
    发生的（preflight 报 all resolved，投影把一条没配的 route 写给了 Claude Code）。

    这里不判「端点是不是真的答话」——那要发请求，属于 preflight `--live`。
    """
    return missing_required_env(profile, env) + missing_credentials(profile, env)


def usable_candidates(config: dict[str, Any], role: str,
                      env: dict[str, str] | None = None) -> list[str]:
    """这个角色现在真能用的候选，按配置顺序。

    三道判据，和 preflight 逐条对应：这一层认得它的方言（`DIALECTS` ↔ preflight 的
    `PROBES`）、角色没有把方言钉死到别处（`_requires_api`）、要的东西当前环境里都有
    （`unmet_requirements`）。
    """
    profiles = as_profiles(config)
    allowed = (config.get("roles", {}).get(role) or {}).get("_requires_api")
    if isinstance(allowed, str):
        allowed = [allowed]
    ready = []
    for name in role_candidates(config, role):
        profile = profiles.get(name)
        if profile is None or profile.get("api") not in DIALECTS:
            continue
        if allowed and profile.get("api") not in allowed:
            continue
        if unmet_requirements(profile, env):
            continue
        ready.append(name)
    return ready


class NoRoute(LookupError):
    """配置里没有这个模型，或者它的方言这一层不支持。"""


def profile_identity(profile: dict[str, Any], *, env: dict[str, str] | None = None) -> str:
    """Return the semantic model identity used for independence checks."""
    return str(profile.get("distinct_model") or resolve(profile, "model", env) or "").strip().lower()


def model_identity(config: dict[str, Any], model: str,
                   *, env: dict[str, str] | None = None) -> str:
    profile = as_profiles(config).get(model)
    return profile_identity(profile, env=env) if profile else model.strip().lower()


def dispatch_profile(profile: dict[str, Any], prompt: str = PROBE_PROMPT,
                     *, alias: str = "", timeout: float = DEFAULT_MODEL_TIMEOUT,
                     contract: str | None = None,
                     env: dict[str, str] | None = None,
                     attempts: int | None = None,
                     retry_delay_seconds: float | None = None,
                     **params) -> Reply:
    """Send one resolved profile; preflight and runtime both use this path."""
    dialect = DIALECTS.get(profile.get("api", ""))
    if dialect is None:
        raise NoRoute(f"模型 {alias or '<unknown>'} 说的是 {profile.get('api')!r}，这一层还不支持")
    endpoint = endpoint_for(profile, timeout=timeout, contract=contract, env=env)
    endpoint["transport"] = profile.get("transport", "httpx")
    effective_params = dict(profile.get("dialect") or {})
    effective_params.update(params)
    limit = attempts if attempts is not None else int(
        profile.get("retry_attempts", api_retry.DEFAULT_ATTEMPTS))
    base = (retry_delay_seconds if retry_delay_seconds is not None
            else float(profile.get("retry_delay_seconds", 1.0)))
    return with_retries(
        lambda: call(
            dialect,
            endpoint,
            str(resolve(profile, "model", env) or alias),
            prompt,
            **effective_params,
        ),
        limit=limit,
        base=base,
    )


def dispatch(config: dict[str, Any], model: str, prompt: str,
             *, timeout: float = DEFAULT_MODEL_TIMEOUT, env: dict[str, str] | None = None,
             attempts: int | None = None,
             retry_delay_seconds: float | None = None, **params) -> Reply:
    """按模型名从配置里解析出一条路并发出去。"""
    profiles = as_profiles(config)
    profile = profiles.get(model)
    if profile is None:
        raise NoRoute(f"配置里没有模型 {model}。现有：{', '.join(sorted(profiles))}")
    return dispatch_profile(
        profile,
        prompt,
        alias=model,
        timeout=timeout,
        env=env,
        attempts=attempts,
        retry_delay_seconds=retry_delay_seconds,
        **params,
    )


def role_candidates(config: dict[str, Any], role: str) -> list[str]:
    """这个角色的候选，两种格式下都答得出来。"""
    entry = config.get("roles", {}).get(role, {})
    return list(entry.get("models") or entry.get("candidates") or [])


def request_params(config: dict[str, Any], role: str | None = None) -> dict[str, Any]:
    """Return the global request defaults with an optional role override.

    Model routes own wire-shape details such as ``token_param``. Roles own workload details such
    as the output budget. Keeping those axes separate lets the same deployment serve a short
    screening role and a long-form planning role without duplicating the model declaration.
    """
    params = dict(config.get("request_defaults") or {})
    if role is not None:
        entry = config.get("roles", {}).get(role, {}) or {}
        params.update(entry.get("request") or {})

    if "max_tokens" in params:
        value = params["max_tokens"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            location = f"roles.{role}.request.max_tokens" if role else "request_defaults.max_tokens"
            raise ValueError(f"{location} 必须是正整数")
    return params


def max_concurrency(config: dict[str, Any]) -> int:
    """Return the global cap for independent model requests."""
    execution = config.get("execution") or {}
    value = execution.get("max_concurrency", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("execution.max_concurrency 必须是正整数")
    return value


def referenced_env_names(config: dict[str, Any]) -> set[str]:
    """Return every environment variable named by either provider schema."""
    names: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "requires_env" and isinstance(value, dict):
                    names.update(str(name) for name in value)
                elif (key == "credential_env" or key == "reject_env"
                      or key.endswith("_env_fallback")):
                    values = [value] if isinstance(value, str) else value
                    if isinstance(values, list):
                        names.update(str(name) for name in values if isinstance(name, str))
                elif key.endswith("_env") and isinstance(value, str):
                    names.add(value)
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(config)
    return names


# ---------------------------------------------------------------------------
# 「这台机器上现在生效的是哪份配置」——只写一处。
#
# Preflight reads the ignored providers.local.json over tracked defaults. The projection must use
# the same effective tree; reading tracked defaults alone previously validated one route and wrote
# another into the official CLI settings.
# ---------------------------------------------------------------------------

def _merge_across_formats(local: dict[str, Any], defaults: dict[str, Any]) -> tuple[dict, list[str]]:
    """一边 v1 一边 v2 时，摊平到 v1 空间再合并。

    往下摊而不是往上转：`as_profiles` 是这个仓里唯一经过验证的换算方向，而且所有读者
    都走它。反过来要在运行时跑迁移器，把一个只读路径变成会改结构的路径。

    The flattened compatibility view uses the first route; two v2 inputs retain full routes.
    """
    profiles = dict(as_profiles(defaults))
    profiles.update(as_profiles(local))

    roles = dict(defaults.get("roles", {}))
    roles.update(local.get("roles", {}))

    merged: dict[str, Any] = {k: v for k, v in defaults.items()
                              if k not in ("roles", "profiles", "endpoints", "models")}
    merged.update({k: v for k, v in local.items()
                   if k not in ("roles", "profiles", "endpoints", "models")})
    merged["version"] = 1
    merged["profiles"] = profiles
    merged["roles"] = roles

    added = [f"{label} {name}" for label, combined, source in
             (("profile", profiles, as_profiles(local)), ("role", roles, local.get("roles", {})))
             for name in combined if name not in source and not name.startswith("_")]
    return merged, added


def merge_over_defaults(local: dict[str, Any], defaults: dict[str, Any]) -> tuple[dict, list[str]]:
    """把本地配置盖在 tracked 默认值上，按 role / profile 逐项合并。

    不是整份替换：装机时复制一次模板之后，上游新加的角色永远到不了那台机器。合并是逐键的，
    操作者写过的条目原样保留，只补他从没见过的。返回补了什么，好让调用方说出来而不是
    悄悄改变行为。

    两边格式不同时先摊到同一个空间再合并。tracked 换成 v2 之后，一台还留着 v1
    `providers.local.json` 的机器会得到「local 的 profiles + default 的 endpoints」，
    `is_v2` 看到 endpoints 就为真，于是 `as_profiles` 只读 models——本机声明的 9 个
    profile 里 5 个整个消失，另外 4 个同名的被 default 的值顶替。实测过，那台机器的
    Azure 配置就这么没了，而且不报任何错。

    「v1 local + v2 default」是所有旧机器升级后的正常状态，所以这里不能报错，只能
    正确合并。
    """
    if is_v2(local) != is_v2(defaults):
        return _merge_across_formats(local, defaults)

    merged = dict(defaults)
    merged.update({k: v for k, v in local.items()
                   if k not in ("roles", "profiles", "endpoints", "models")})
    added: list[str] = []
    for section in ("roles", "profiles", "endpoints", "models"):
        if section not in defaults and section not in local:
            continue
        combined = dict(defaults.get(section, {}))
        combined.update(local.get(section, {}))
        added += [f"{section.rstrip('s')} {name}" for name in combined
                  if name not in local.get(section, {}) and not name.startswith("_")]
        merged[section] = combined
    return merged, added


def load_effective_config(repo_root: Path, explicit: Path | None = None
                          ) -> tuple[dict[str, Any], Path, list[str]]:
    """现在生效的配置，以及它从哪来、补进了什么。

    显式给了路径就用那一份，别的都不看——`--config x` 的意思就是 x。没给的话，本地那份
    盖在 tracked 默认值上；本地不存在时就是 tracked 那份。

    preflight、投影和迁移器都调它，所以它们不可能再看着两份不同的配置各说各话。
    """
    tracked = repo_root / "config" / "providers.example.json"
    local = repo_root / "config" / "providers.local.json"

    if explicit is not None:
        return json.loads(explicit.read_text(encoding="utf-8")), explicit, []
    if not local.exists():
        return json.loads(tracked.read_text(encoding="utf-8")), tracked, []

    merged, added = merge_over_defaults(
        json.loads(local.read_text(encoding="utf-8")),
        json.loads(tracked.read_text(encoding="utf-8")) if tracked.exists() else {})
    return merged, local, added
