"""一个请求要不要走代理、走哪个代理，由它的消费者决定，不是由发请求的人决定。

这个仓库里有两套代理契约，因为不同 route 对网络环境的假设不同：

  AUTORESEARCH  src/llm_client.py 的所有 Python 调用。读 AUTORESEARCH_PROXY_URL
                （默认 127.0.0.1:7890），并且 trust_env=False 明确无视标准代理变量。
                所有 googleapis.com 端点强制走它，注释里记着实测直连不通。

  STANDARD_ENV  标成 standard-env 的 Python provider route。这里读取 https_proxy /
                HTTPS_PROXY / http_proxy / HTTP_PROXY 和 no_proxy / NO_PROXY，再把选中的
                代理显式交给 httpx。reviewer、critic 与 monitor 都经同一 Python role bridge。

官方 Claude Code CLI 自身继承 shell 环境，不由本模块治理；本模块只负责 AutoResearch
Python dispatcher 与 preflight 共同使用的 route 合同。

  DIRECT        明确不走代理，并且不受 env 影响。

preflight 要探的是"消费者实际会走的那条路"，所以它必须按 profile 各自的契约来。用一个
统一的 needs_proxy 开关会造出新的假绿：preflight 与实际 route 读取不同变量时，一侧可用
不代表另一侧会走同一网络路径。

这个模块只做决策，不发请求，所以能用假代理整体驱动。
"""

from __future__ import annotations

import os
import re
import socket
import time
from urllib.parse import urlparse

AUTORESEARCH = "autoresearch"
STANDARD_ENV = "standard-env"
DIRECT = "direct"

CONTRACTS = (AUTORESEARCH, STANDARD_ENV, DIRECT)

DEFAULT_AUTORESEARCH_PROXY = "http://127.0.0.1:7890"

# 代理活着与否用一次 TCP 连接判断，不发 HTTP。1.5s 足够判断本机或同网段的端口，
# 又不会在代理不存在时把整体探测拖长。
LIVENESS_TIMEOUT = 1.5


class UnknownContract(ValueError):
    """profile 写了一个不存在的契约名。静默当成直连会让它在受限网络上假绿。"""


def _env(env):
    return os.environ if env is None else env


def _host_of(url):
    parsed = urlparse(url if "://" in url else f"//{url}")
    return parsed.hostname or "", parsed.port


def hostname_is_within(url, domain):
    """Return whether a parsed hostname is a domain or one of its subdomains."""
    try:
        value = str(url)
        parsed = urlparse(value if "://" in value else f"//{value}")
        hostname = (parsed.hostname or "").rstrip(".").lower()
    except (TypeError, ValueError):
        return False

    domain = str(domain).rstrip(".").lower()
    return bool(domain) and (hostname == domain or hostname.endswith(f".{domain}"))


def contract_for_legacy_base_url(base_url):
    """Infer the retired preset contract without treating URL text as a hostname."""
    if hostname_is_within(base_url, "googleapis.com"):
        return AUTORESEARCH
    return DIRECT


def bypasses_proxy(target_url, no_proxy):
    """NO_PROXY 命中就直连。

    这是 STANDARD_ENV 在仓内的规范化子集：

      - 整个值就是 `*` 时全放行（列表里的一项 `*` 不算）
      - 逗号或空白分隔
      - 条目带 `:` 时按 host:port 精确比，端口缺省按协议补 443 / 80
      - 条目以 `.` 开头时按后缀匹配，并且也匹配去掉点之后的主机本身
      - 其余按主机名精确匹配

    最后一条容易想当然：`NO_PROXY=example.com` 不会放行 `api.example.com`，要写
    `.example.com`。第一版按后缀匹配写，被测试抓了出来。

    requests 和 Bun 的 NO_PROXY 语义在边角上和这份不完全一致。三者都认的写法是
    「精确主机名」和「点开头的后缀」，配置里只用这两种就不会遇到分歧。
    """
    if not no_proxy:
        return False
    if no_proxy.strip() == "*":
        return True
    host, port = _host_of(target_url)
    host = (host or "").lower()
    if not host:
        return False
    if port is None:
        port = 443 if str(target_url).startswith("https") else 80
    host_with_port = f"{host}:{port}"
    for raw in re.split(r"[,\s]+", no_proxy):
        entry = raw.strip().lower()
        if not entry:
            continue
        if ":" in entry:
            if host_with_port == entry:
                return True
        elif entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def proxy_url_for(contract, target_url, env=None):
    """这条请求应该经过哪个代理，None 表示直连。"""
    if contract not in CONTRACTS:
        raise UnknownContract(
            f"未知的 proxy_contract: {contract!r}。可用的是 {', '.join(CONTRACTS)}。"
            f"写错名字时按直连处理，会在受限网络上把不通的配置报成通。"
        )
    if contract == DIRECT:
        return None
    environ = _env(env)
    if contract == AUTORESEARCH:
        return environ.get("AUTORESEARCH_PROXY_URL") or DEFAULT_AUTORESEARCH_PROXY

    # STANDARD_ENV keeps the existing lowercase-first precedence.
    if bypasses_proxy(target_url, environ.get("no_proxy") or environ.get("NO_PROXY")):
        return None
    return (
        environ.get("https_proxy") or environ.get("HTTPS_PROXY")
        or environ.get("http_proxy") or environ.get("HTTP_PROXY")
        or None
    )


_ALIVE_CACHE: dict = {}
ALIVE_TTL = 20.0


def proxy_alive(proxy_url, timeout=LIVENESS_TIMEOUT):
    """代理端口有没有在监听。结果缓存 ALIVE_TTL 秒。

    缓存是因为一次探测里 describe 和 effective_kwargs 会各问一遍，每个候选都做一次
    TCP 连接会给整体平白加上秒数。
    """
    if not proxy_url:
        return True  # 直连没有代理可探，不构成阻碍
    cached = _ALIVE_CACHE.get(proxy_url)
    if cached and time.monotonic() - cached[0] < ALIVE_TTL:
        return cached[1]
    host, port = _host_of(proxy_url)
    if not host:
        return False
    if port is None:
        port = 443 if proxy_url.startswith("https") else 80
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            ok = sock.connect_ex((host, port)) == 0
    except OSError:
        ok = False
    _ALIVE_CACHE[proxy_url] = (time.monotonic(), ok)
    return ok


def is_explicit(contract, env=None):
    """操作者有没有亲口指定这个契约的代理。

    AUTORESEARCH 的默认值 127.0.0.1:7890 只是对开发机的假设。没设变量、端口又没人听
    时，说明这台机器不需要代理（境外 VPS、RunPod），直连才是对的；设过就是明确表态，
    代理死了要明确失败，不能偷偷直连：那会把「隧道断了」变成一串 TLS 错误。
    """
    environ = _env(env)
    if contract == AUTORESEARCH:
        return "AUTORESEARCH_PROXY_URL" in environ
    if contract == STANDARD_ENV:
        return any(k in environ for k in
                   ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"))
    return False


def effective_kwargs(contract, target_url, *, timeout, env=None):
    """这条请求实际怎么发，或者 None 表示「明确要求的代理不可用」。

    preflight 和 llm_client 共用这一个函数。两边各判一次，就会在不需要代理的机器上
    出现「preflight 报 transport 失败、消费者直连成功」这种新的分叉。
    """
    proxy = proxy_url_for(contract, target_url, env=env)
    if not proxy or proxy_alive(proxy):
        return httpx_kwargs(contract, target_url, timeout=timeout, env=env)
    if is_explicit(contract, env=env):
        return None
    return httpx_kwargs(DIRECT, target_url, timeout=timeout, env=env)


def httpx_kwargs(contract, target_url, *, timeout, env=None):
    """给 httpx.Client 的 kwargs。

    trust_env 永远是 False：代理由这里显式决定。让 httpx 自己读 env，AUTORESEARCH
    契约就会在设了 HTTPS_PROXY 的机器上悄悄改道，而消费者不会。
    """
    kwargs = {"timeout": timeout, "trust_env": False}
    proxy = proxy_url_for(contract, target_url, env=env)
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def describe(contract, target_url, env=None):
    """给人看的一行：这条请求实际会怎么走。

    照 proxy_url_for 打是错的：不受限主机上默认 7890 是死的、变量也没设，请求会直连，
    而标签会写成经由那个代理，和实际发生的事相反。
    """
    kwargs = effective_kwargs(contract, target_url, timeout=1, env=env)
    if kwargs is None:
        return f"经 {proxy_url_for(contract, target_url, env=env)}（不可用）"
    proxy = kwargs.get("proxy")
    return f"经 {proxy}" if proxy else "直连"
