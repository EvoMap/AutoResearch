"""LLM 统一调用客户端。

运行时配置来自 config/providers.local.json（没有时使用 tracked 模板）。JSON 只保存
endpoint / model / role 的代称和环境变量名；真实 URL 与 key 留在环境变量里。

每个 preset 只调用操作者声明的端点。需要代理的端点通过 needs_proxy 与
AUTORESEARCH_PROXY_URL 配置；所有调用强制 trust_env=False，避免环境变量污染。
"""

import json
import os
import httpx
import time as _global_time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import api_retry
import providers
import proxy_contract
import roles

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_CONFIG_PATH = REPO_ROOT / "config.local.json"
LEGACY_EXAMPLE_PATH = REPO_ROOT / "config.example.json"
# 操作者有没有明确表态，决定代理不通时是失败还是直连。显式设过就是「我在受限网络」，
# 没设时那个默认值只是对开发机的假设，端口没人听说明假设不成立。
def _timeout_from_env(default=900):
    """一次调用最多等多久。

    Step 1 的提示词把整份领域知识文件注进去（单份 10–24KB）再要 2500 token 输出，
    实测一个短得多的请求就要 59 秒。原来写死 120，2026-08-10 的第一次真实跑批里
    三个构思席位有一个就这么超时掉了。

    写错值不该让整条管线起不来：这个变量会被写进 .env。
    """
    raw = os.environ.get("AR_LLM_TIMEOUT")
    try:
        value = float(raw) if raw else default
    except ValueError:
        print(f"  [config] AR_LLM_TIMEOUT={raw!r} 不是数字，用默认 {default}s")
        return default
    return value if value > 0 else default


LLM_TIMEOUT = _timeout_from_env()


def _request_max_tokens(value=None):
    """Resolve the low-level compatibility callers through the provider JSON too."""
    if value is not None:
        return value
    configured = providers.request_params(_provider_config()).get("max_tokens")
    if configured is None:
        raise RuntimeError("provider 配置缺少 request_defaults.max_tokens")
    return configured

PROXY_EXPLICIT = "AUTORESEARCH_PROXY_URL" in os.environ
PROXY_URL = os.environ.get("AUTORESEARCH_PROXY_URL", proxy_contract.DEFAULT_AUTORESEARCH_PROXY)
_proxy = urlparse(PROXY_URL)
_PROXY_HOST = _proxy.hostname or "127.0.0.1"
_PROXY_PORT = _proxy.port or 7890


_PROXY_CACHE = {"checked_at": 0.0, "alive": None}


def proxy_alive(ttl_seconds=20):
    """快速 TCP 探测代理端口是否监听；结果缓存 ttl_seconds 秒。

    判断逻辑在 proxy_contract 里，preflight 用的是同一份。两边各写一遍就会分叉，
    而分叉的表现是 preflight 绿、真实调用红。
    """
    now = _global_time.time()
    if _PROXY_CACHE["alive"] is not None and now - _PROXY_CACHE["checked_at"] < ttl_seconds:
        return _PROXY_CACHE["alive"]
    ok = proxy_contract.proxy_alive(PROXY_URL)
    _PROXY_CACHE["alive"] = ok
    _PROXY_CACHE["checked_at"] = now
    return ok


def _httpx_kwargs(needs_proxy, *, timeout=None):
    """
    返回 httpx.Client 的标准 kwargs。
    - needs_proxy=True：仅在代理活着时加 proxy；代理死时返回 None（上层应跳过/降级）
    - needs_proxy=False：明确不走代理，且 trust_env=False 避免 env 污染
    """
    contract = proxy_contract.AUTORESEARCH if needs_proxy else proxy_contract.DIRECT
    return proxy_contract.effective_kwargs(
        contract, "", timeout=LLM_TIMEOUT if timeout is None else timeout)


def _post_with_retries(url, *, headers, payload, needs_proxy=False,
                       timeout=None, attempts=api_retry.DEFAULT_ATTEMPTS,
                       retry_delay=5.0, label="模型 API"):
    """旧 preset 路径也使用与统一 provider 相同的重试内核。"""
    kwargs = _httpx_kwargs(needs_proxy=needs_proxy, timeout=timeout)
    if kwargs is None:
        return None

    def send():
        with httpx.Client(**kwargs) as client:
            return client.post(url, headers=headers, json=payload)

    try:
        return api_retry.retry_http(
            send,
            attempts=attempts,
            retry_delay=retry_delay,
            operation_name=label,
        )
    except api_retry.RetryError as exc:
        print(f"  [{label}] {exc}")
        return None
    except Exception as exc:                          # noqa: BLE001
        print(f"  [{label}] 请求配置错误: {type(exc).__name__}: {exc}")
        return None


def load_config():
    """Return the one effective runtime configuration.

    AUTORESEARCH_CONFIG is an explicit path override for tests and one-off runs. The
    normal local file is config/providers.local.json; providers.load_effective_config
    overlays it on the tracked template so new roles still reach existing installs.
    """
    explicit = os.environ.get("AUTORESEARCH_CONFIG")
    path = Path(explicit).expanduser() if explicit else None
    return providers.load_effective_config(REPO_ROOT, path)[0]


def load_legacy_config():
    """Read the retired v1 preset file for aliases not declared by unified config."""
    path = LEGACY_CONFIG_PATH if LEGACY_CONFIG_PATH.exists() else LEGACY_EXAMPLE_PATH
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def get_preset(name):
    config = load_legacy_config()
    return config.get("presets", {}).get(name)


def _cfg_value(cfg, key, default=None):
    """Read a config value directly or from an environment-variable indirection.

    Public templates use fields such as api_key_env/base_url_env so users can
    keep secrets out of Git. Private legacy configs with direct api_key/base_url
    values continue to work.
    """
    env_key = cfg.get(f"{key}_env")
    if env_key:
        value = os.environ.get(env_key)
        if value:
            return value
    return cfg.get(key, default)


def _require_cfg_value(cfg, key, preset_name):
    value = _cfg_value(cfg, key)
    if value:
        return value
    env_key = cfg.get(f"{key}_env")
    suffix = f" or set ${env_key}" if env_key else ""
    raise RuntimeError(f"Preset '{preset_name}' is missing '{key}'{suffix}")


def _resolve_alias(name):
    """Map public short aliases to concrete preset names."""
    config = load_legacy_config()
    aliases = config.get("aliases", {}) or config.get("_aliases", {})
    return aliases.get(name, name)


def _resolve_claude_preset(name):
    """把短名映射到真实 preset 名"""
    return _resolve_alias(name)


# ============================================================
# Gemini 系列（OpenAI 兼容格式，需代理）
# ============================================================
def call_chat_completions(prompt, preset_name="gemini-3.1-flash-lite", temperature=0.3,
                          max_tokens=None, attempts=api_retry.DEFAULT_ATTEMPTS):
    """任何 OpenAI Chat Completions 兼容 preset 都走这里。

    原名叫 call_gemini，但它从来不是 Gemini 专用的：它按 preset 拼 /chat/completions、
    Bearer、messages。名字里写死一家厂商，会让下一个人以为换模型得再写一个函数。
    """
    cfg = get_preset(preset_name)
    if not cfg:
        return None
    max_tokens = _request_max_tokens(max_tokens)

    base_url = _cfg_value(cfg, "base_url", default="")
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_require_cfg_value(cfg, 'api_key', preset_name)}",
    }
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # Gemini 在受限网络下必须走代理；config 字段 needs_proxy 默认 False，
    # 但实测官方 generativelanguage.googleapis.com 直连不通 → 这里强制按 True 处理
    needs_proxy = bool(cfg.get("needs_proxy", False)) or (
        proxy_contract.contract_for_legacy_base_url(base_url)
        == proxy_contract.AUTORESEARCH
    )

    resp = _post_with_retries(
        url,
        headers=headers,
        payload=payload,
        needs_proxy=needs_proxy,
        attempts=attempts,
        label=preset_name,
    )
    if resp is None:
        print(f"  [{preset_name}] 请求失败；若本机不需要代理，请不要设置 AUTORESEARCH_PROXY_URL")
        return None
    if resp.status_code != 200:
        print(f"  [{preset_name}] HTTP {resp.status_code}: {resp.text[:100]}")
        return None
    try:
        content = resp.json()["choices"][0]["message"].get("content")
    except (KeyError, TypeError, ValueError) as exc:
        print(f"  [{preset_name}] 返回格式错误: {exc}")
        return None
    if content is None:
        print(f"  [{preset_name}] 无输出内容（max_tokens 过小或全用于 thinking）")
        return None
    return content


# ============================================================
# Claude 系列（Anthropic Messages API 或兼容网关）
# ============================================================
def call_claude(prompt, preset_name="claude-sonnet-4-6", temperature=0.3,
                max_tokens=None, attempts=api_retry.DEFAULT_ATTEMPTS):
    resolved = _resolve_claude_preset(preset_name)
    max_tokens = _request_max_tokens(max_tokens)
    cfg = get_preset(resolved)
    if not cfg:
        print(f"  [Claude] 未找到 preset: {resolved}")
        return None

    if cfg.get("provider") not in ("anthropic_gateway", "anthropic_messages"):
        print(f"  [Claude] preset {resolved} 不是 Anthropic Messages 兼容配置，请更新本地配置")
        return None

    url = _cfg_value(cfg, "base_url", default="").rstrip("/") + "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_require_cfg_value(cfg, 'auth_token', resolved)}",
        "anthropic-version": "2023-06-01",
        "User-Agent": "curl/7.88.1",
    }
    payload = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }

    resp = _post_with_retries(
        url,
        headers=headers,
        payload=payload,
        needs_proxy=cfg.get("needs_proxy", False),
        attempts=attempts,
        label=f"Claude/{cfg['model']}",
    )
    if resp is None:
        return None
    if resp.status_code != 200:
        print(f"  [Claude/{cfg['model']}] HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    try:
        data = resp.json()
    except ValueError as exc:
        print(f"  [Claude/{cfg['model']}] 返回格式错误: {exc}")
        return None
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    return None


# ============================================================
# GPT-5.5（OpenAI Chat Completions API 或兼容网关）
# ============================================================
def call_gpt(prompt, temperature=0.3, max_tokens=None,
             attempts=api_retry.DEFAULT_ATTEMPTS):
    return call_chat_completions(
        prompt,
        "gpt-5.5",
        temperature=temperature,
        max_tokens=max_tokens,
        attempts=attempts,
    )


# ============================================================
# 便捷接口（按任务复杂度选模型）
# ============================================================
def call_flash(prompt, **kwargs):
    """简单任务：优先 Gemini Flash Lite，失败时回落 GPT-5.5。"""
    result = call_chat_completions(prompt, "gemini-3.1-flash-lite", **kwargs)
    if result:
        return result
    print("  [Flash] Gemini 不可用，回落 GPT-5.5")
    return call_gpt(prompt, **kwargs)


def call_pro(prompt, **kwargs):
    """综合研判：优先 Gemini 3.1 Pro，失败时回落 GPT-5.5。"""
    kwargs.setdefault("temperature", 0.2)
    kwargs.setdefault("max_tokens", _request_max_tokens())
    result = call_chat_completions(prompt, "gemini-3.1-pro", **kwargs)
    if result:
        return result
    print("  [Pro] Gemini 不可用，回落 GPT-5.5")
    return call_gpt(prompt, **kwargs)


def first_hop(model_name):
    """这个名字的请求，第一个真的会被发出去的 preset 是谁。"""
    if model_name in _provider_models():
        return model_name
    if model_name in ("gpt-5.5", "gemini-flash", "gemini-pro"):
        # 这三个最终都落到 call_gpt（flash/pro 是先 Gemini 后回落 GPT）。
        if model_name == "gemini-flash" and get_preset("gemini-3.1-flash-lite"):
            return "gemini-3.1-flash-lite"
        if model_name == "gemini-pro" and get_preset("gemini-3.1-pro"):
            return "gemini-3.1-pro"
        return "gpt-5.5"
    if model_name in ("claude-opus", "claude-sonnet", "claude-haiku"):
        return _resolve_claude_preset(model_name)
    preset_name, _ = _preset_for(model_name)
    return preset_name


class UnknownRole(ValueError):
    """这个名字不是任何角色。"""


# 每个角色的推荐模型经过实测，但不构成强制要求；
# AR_MODEL_<ROLE> 换掉它，见 src/roles.py。
# 每个角色在没有配置、也没有环境变量时的兜底名字。它是第三顺位：
#
#     AR_MODEL_<ROLE>  >  providers 配置里那个角色的候选列表  >  这里
#
# 配置里已经写了每个角色三到五个候选，那才是回落链的所在。这张表只在配置读不出来
# 时用（比如别人拿走 src/ 单独用），所以它给的是单个名字，不是链。
ROLE_DEFAULTS = {
    "screener": "gemini-flash",
    "judge": "gemini-pro",
    "consensus_checker": "gemini-pro",
    "ideator": ["gemini-pro", "gpt-5.5", "claude-opus"],
    "planner": "gpt-5.5",
    "freshness_refresher": "claude-opus",
}

def _configured_candidates(role, config=None):
    """配置里这个角色的候选。读不出来就返回空，交给 ROLE_DEFAULTS 兜底。

    优先读配置而不是 ROLE_DEFAULTS：那张表写的是单个名字，而配置写的是链。用表的话
    preflight 逐个探过的备选在运行时一个都到不了。
    """
    try:
        source = config if config is not None else _provider_config()
        return providers.role_candidates(source, role)
    except (OSError, ValueError):
        return []


def configured_role_models(role, config=None):
    """Models the runtime will use for a role, in effective-config order."""
    configured = (_configured_candidates(role, config) if config is not None
                  else _configured_candidates(role))
    if not configured and role not in ROLE_DEFAULTS:
        source = config if config is not None else load_config()
        known_roles = sorted(set(source.get("roles", {})) | set(ROLE_DEFAULTS))
        raise UnknownRole(
            f"没有叫 {role} 的角色。现有：{', '.join(known_roles)}")
    return roles.models_for(role, configured or ROLE_DEFAULTS[role])


def configured_distinct_role_models(role, config=None):
    """Models a panel will call, collapsed by semantic model identity."""
    source = config if config is not None else _provider_config()
    models = configured_role_models(role, config) if config is not None else configured_role_models(role)
    distinct = []
    seen = set()
    for model in models:
        identity = providers.model_identity(source, model)
        if identity in seen:
            continue
        seen.add(identity)
        distinct.append(model)
    return distinct


def candidates_for(role):
    """Compatibility name for the effective role candidate list."""
    return configured_role_models(role)


def configured_request_params(role=None, config=None):
    """Request budget from the same effective JSON used for model routing."""
    source = config if config is not None else _provider_config()
    return providers.request_params(source, role)


def configured_max_concurrency(config=None):
    source = config if config is not None else load_config()
    return providers.max_concurrency(source)


@dataclass(frozen=True)
class RoleCallResult:
    role: str
    model: str
    model_identity: str
    text: str


def call_role_result(role, prompt, *, exclude_identities=(), **kwargs):
    """Call a role and retain the model that actually produced the answer.

    Candidate lists describe possible routes, not the route that answered. Review panels use the
    returned identity to avoid counting a fallback or an alias of the same model twice.
    """
    config = _provider_config()
    candidates = configured_role_models(role, config)
    params = {**configured_request_params(role, config), **kwargs}
    excluded = {str(identity).strip().lower() for identity in exclude_identities}

    # 逐个试，不是只用第一个。配置给每个角色写了三到五个候选，preflight 也逐个探过。
    # 而运行时原来只取 `[0]`，于是首选挂掉时整个角色就没了，尽管配置里明明还有备选。
    # `_LEGACY` 里那几条写死的回落链（gemini-flash 失败回落 gpt-5.5 之类）就是在补这个
    # 洞：一份写在代码里、只覆盖六个名字的回落表。回落属于配置。
    last_error = None
    for index, model in enumerate(candidates):
        identity = providers.model_identity(config, model)
        if identity in excluded:
            continue
        try:
            result = call_model(model, prompt, _config=config, **params)
        except UnknownModel:
            raise
        except Exception as exc:                      # noqa: BLE001
            last_error = exc
            result = None
        if result:
            if index:
                print(f"  [{role}] 前 {index} 个候选没给出结果，用的是 {model}")
            return RoleCallResult(role, model, identity, result)

    if last_error is not None and len(candidates) == 1:
        # 只有一个候选时，把原始异常交出去。吞掉它等于把「端点挂了」说成「模型没话说」。
        raise last_error
    return None


def call_role(role, prompt, **kwargs):
    """按角色调用。调用点说用途，模型是数据。"""
    result = call_role_result(role, prompt, **kwargs)
    return result.text if result else None


def call_role_with_model(role, prompt, **kwargs):
    """Return the role text and the model alias that actually answered."""
    result = call_role_result(role, prompt, **kwargs)
    return (result.text, result.model) if result else (None, None)


class UnknownModel(ValueError):
    """这个名字既不是 preset，也不是任何 preset 的模型。"""


# 几个带回落逻辑的老名字。它们不是模型标识，是「这一类任务用什么」的简写，散在配置和
# 提示词里，所以保留；新名字一律走配置。
_LEGACY = {
    "gemini-pro": lambda prompt, **kw: call_pro(prompt, **kw),
    "gemini-flash": lambda prompt, **kw: call_flash(prompt, **kw),
    "claude-opus": lambda prompt, **kw: call_claude(prompt, "claude-opus", **kw),
    "claude-sonnet": lambda prompt, **kw: call_claude(prompt, "claude-sonnet", **kw),
    "claude-haiku": lambda prompt, **kw: call_claude(prompt, "claude-haiku", **kw),
    "gpt-5.5": lambda prompt, **kw: call_gpt(prompt, **kw),
}

_ANTHROPIC = ("anthropic_messages", "anthropic_gateway")


def _preset_for(model_name):
    """名字对应哪个 preset：先当 preset 名，再当某个 preset 的模型名。"""
    presets = load_legacy_config().get("presets", {})
    if model_name in presets:
        return model_name, presets[model_name]
    for key, cfg in presets.items():
        if cfg.get("model") == model_name:
            return key, cfg
    return None, None


def _provider_config():
    return load_config()


def _provider_models(config=None):
    try:
        return providers.as_profiles(config if config is not None else _provider_config())
    except (OSError, ValueError):
        return {}


class ModelUnreachable(RuntimeError):
    """名字解析得出来，这次请求没成。跟「没有这个模型」是两回事。

    刻意不是 `UnknownModel` 的子类：那一档的约定是「配置写错了，换个候选也救不了」，
    调用点据此原样抛出去。端点连不上归到那一档的话，一个连不上的席位会带走整轮构思。
    """


def _via_providers(model_name, prompt, *, config=None, **kwargs):
    """从 providers 配置解析这个名字并发出去。名字在不在，由调用点先判。"""
    config = config if config is not None else _provider_config()
    attempts = kwargs.get("attempts")
    retry_delay_seconds = kwargs.get("retry_delay_seconds")
    params = {
        **providers.request_params(config),
        **{k: v for k, v in kwargs.items()
           if k in ("temperature", "max_tokens", "system")},
    }
    reply = providers.dispatch(
        config,
        model_name,
        prompt,
        timeout=kwargs.get("timeout", LLM_TIMEOUT),
        attempts=attempts,
        retry_delay_seconds=retry_delay_seconds,
        **params,
    )
    if reply.ok:
        return reply.text
    if reply.outcome == providers.EMPTY:
        # 200、形状对、文本是空的。调用点约定拿不到内容就是 None，preset 那条路也是这样。
        return None
    # 剩下的都是这次没发出去：DNS、401、429、502。原来跟「配置里没这个名字」共用一个
    # None，于是 call_model 改口说这个模型不存在，还把它列进「现在可用」，而真正的原因
    # 在 Reply.detail 里被丢掉，排错方向被带去查配置（#194）。
    raise ModelUnreachable(
        f"{model_name} 在 {len(reply.attempts)} 次尝试后没发出去："
        f"{reply.outcome}（{reply.stage}）{reply.detail}")


def call_model(model_name, prompt, *, _config=None, **kwargs):
    """按名字调用一个模型。

    名字不必在这个文件里出现过。角色的模型是配置里的数据（见 src/roles.py），写死一张
    名字表的话，`AR_MODEL_JUDGE=<别的模型>` 会让 preflight 变绿而这里静默返回 None，
    调用方拿到 None 只会当成「模型没话说」，一路流到判定为空。
    """
    config = _config if _config is not None else _provider_config()
    kwargs = {**providers.request_params(config), **kwargs}

    # Explicit unified configuration wins over retired aliases with the same name.
    # Otherwise a user can point `gemini-pro` at their own gateway, see preflight
    # validate that route, and still have runtime silently call the old Google path.
    if model_name in _provider_models(config):
        return _via_providers(model_name, prompt, config=config, **kwargs)

    if model_name in _LEGACY:
        return _LEGACY[model_name](prompt, **kwargs)

    preset_name, cfg = _preset_for(model_name)
    if cfg:
        if cfg.get("provider") in _ANTHROPIC:
            return call_claude(prompt, preset_name, **kwargs)
        return call_chat_completions(prompt, preset_name, **kwargs)

    # presets 里没有的名字，去 providers 配置里找。preflight 一直按那份解析，运行时
    # 却只认 presets 和上面那张表：本机 18 个 providers 模型里 13 个在这里解析不出来，
    # 于是 `AR_MODEL_JUDGE=oai-mid` 让 preflight 变绿、运行时抛「不认识」。
    known = sorted(set(_LEGACY) | set(_provider_models(_config)) | {
        name for cfg in load_legacy_config().get("presets", {}).values()
        if (name := cfg.get("model"))})
    raise UnknownModel(
        f"没有配置能提供模型 {model_name}。\n"
        f"  现在可用：{', '.join(known)}\n"
        f"  要用别的模型：加进 config/providers.local.json 的 models，"
        f"或 config.local.json 的 presets"
    )


if __name__ == "__main__":
    print("测试所有模型连通性...")
    print(f"  Gemini Flash:        {call_flash('1+1=?', max_tokens=20)}")
    print(f"  GPT-5.5:             {call_gpt('1+1=?', max_tokens=30)}")
    print(f"  Claude Sonnet:       {call_claude('1+1=?', 'claude-sonnet-4-6', max_tokens=20)}")
    print(f"  Claude Opus:         {call_claude('1+1=?', 'claude-opus-4-7', max_tokens=20)}")
