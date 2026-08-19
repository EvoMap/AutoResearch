"""preflight 和真实消费者必须连到同一个地方。这里真开 socket。

这一整套测试存在的原因是 issue #43：preflight 的五个 httpx client 全部写死
`trust_env=False` 且没有代理入口，于是在受限网络的机器上探的是产线从不使用的直连路径，
表现为七次 60 秒静默。本机跑 `--live` 是绿的，因为本机直连本来就通，绿得毫无信息量。

57 条既有 preflight 测试没有一条提到代理，而所谓的端到端测试全都把网络层 mock 掉了：
整个套件从不打开一个 socket，传输这一维按构造就没有覆盖。

所以这里不 mock。起一个真的假代理（接受连接、记下它看到的请求、返回一个能解析的响应），
让 preflight 和消费者各自发一次请求，断言两边都出现在同一个代理的请求记录里。声明写错、
契约分叉、trust_env 又被写死，任意一种都会让这里红。
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import providers  # noqa: E402
import proxy_contract  # noqa: E402


class RecordingProxy(BaseHTTPRequestHandler):
    """一个够用的 HTTP 正向代理：记下请求行，并对普通请求回一个 JSON。

    只处理明文 HTTP。CONNECT 隧道只记录不建立：测试的目标是「这个请求有没有经过我」，
    不是把 TLS 跑通。
    """

    protocol_version = "HTTP/1.1"
    seen: list[str] = []

    def log_message(self, *args):  # 静音，否则 pytest 输出被请求日志淹没
        pass

    def _record(self):
        type(self).seen.append(f"{self.command} {self.path}")

    def do_POST(self):
        self._record()
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({"proxied": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def do_CONNECT(self):
        self._record()
        self.send_response(502)  # 不建隧道；记录到就够了
        self.end_headers()


@pytest.fixture
def proxy():
    """跑起来的假代理，返回 (url, seen)。"""
    RecordingProxy.seen = []
    server = HTTPServer(("127.0.0.1", 0), RecordingProxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", RecordingProxy.seen
    server.shutdown()
    server.server_close()


@contextmanager
def dead_proxy_url():
    """Hold a bound, non-listening socket so the dead proxy port cannot be reused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"


TARGET = "http://upstream.invalid/v1/chat/completions"


# ---- 两套契约各自读自己的变量 ----

def test_the_autoresearch_contract_ignores_the_standard_variables(proxy):
    """llm_client 用 trust_env=False，所以设了 HTTPS_PROXY 也不会改道。

    preflight 如果改成读标准变量，就会在设了 HTTPS_PROXY 的机器上探一条消费者不走的路。
    """
    url, _ = proxy
    env = {"HTTPS_PROXY": "http://127.0.0.1:9", "AUTORESEARCH_PROXY_URL": url}
    assert proxy_contract.proxy_url_for(proxy_contract.AUTORESEARCH, TARGET, env=env) == url


def test_the_standard_contract_ignores_the_autoresearch_variable(proxy):
    """A standard-env route does not read AUTORESEARCH_PROXY_URL."""
    url, _ = proxy
    env = {"AUTORESEARCH_PROXY_URL": url}
    assert proxy_contract.proxy_url_for(proxy_contract.STANDARD_ENV, TARGET, env=env) is None


@pytest.mark.parametrize("no_proxy, expected_proxied", [
    ("upstream.invalid", False),   # 精确主机名
    ("invalid", True),             # 不带点是精确匹配，不是后缀
    (".invalid", False),           # 带点才是后缀
    ("*", False),                  # 整个值是 * 时全放行
    ("a.com,*", True),             # 列表里的一项 * 不算
    ("other.invalid", True),
    ("upstream.invalid:80", False),  # host:port 形式
    ("upstream.invalid:443", True),  # 端口对不上就不放行
])
def test_no_proxy_matches_the_way_the_consumer_matches(proxy, no_proxy, expected_proxied):
    """Rules follow the repository's explicit STANDARD_ENV subset.

    第一版把不带点的条目按后缀匹配，于是 `NO_PROXY=invalid` 会放行
    `upstream.invalid`，而真实现是精确匹配。这条参数化用例就是那次的记录。
    """
    url, _ = proxy
    env = {"HTTPS_PROXY": url, "NO_PROXY": no_proxy}
    got = proxy_contract.proxy_url_for(proxy_contract.STANDARD_ENV, TARGET, env=env)
    assert (got == url) is expected_proxied


def test_an_unknown_contract_is_an_error_not_a_direct_connection():
    """打错名字当成直连，就会在受限网络上把不通的配置报成通。"""
    with pytest.raises(proxy_contract.UnknownContract, match="未知的 proxy_contract"):
        proxy_contract.proxy_url_for("typo", TARGET)


# ---- 真的发一次请求：两边都要落在同一个代理上 ----

def test_a_request_built_from_the_contract_actually_goes_through_the_proxy(proxy):
    """不是断言 kwargs 长什么样，是看假代理有没有收到这个请求。"""
    url, seen = proxy
    kwargs = proxy_contract.httpx_kwargs(
        proxy_contract.AUTORESEARCH, TARGET, timeout=5,
        env={"AUTORESEARCH_PROXY_URL": url})

    with httpx.Client(**kwargs) as client:
        client.post(TARGET, json={"probe": True})

    assert seen, "代理没收到请求，说明这条请求直连了"
    assert TARGET in seen[0]


def test_preflight_and_the_consumer_reach_the_same_proxy(proxy, monkeypatch):
    """这一条是整个文件的目的。

    preflight 声明某个 profile 走 autoresearch 契约，真实消费者 llm_client 也走它。
    两边各发一次请求，假代理必须都看见。任一边改了实现而另一边没跟上，这里就红。
    """
    url, seen = proxy
    monkeypatch.setenv("AUTORESEARCH_PROXY_URL", url)

    import importlib

    import llm_client
    import preflight
    importlib.reload(llm_client)

    # 消费者这一侧
    consumer_kwargs = llm_client._httpx_kwargs(needs_proxy=True, timeout=5)
    assert consumer_kwargs is not None, "假代理是活的，消费者不该判它死"
    with httpx.Client(**consumer_kwargs) as client:
        client.post(TARGET, json={"from": "consumer"})

    # preflight 这一侧，走它自己的 probe_client
    profile = {"proxy_contract": "autoresearch", "base_url": "http://upstream.invalid"}
    with preflight.probe_client(profile, TARGET) as client:
        client.post(TARGET, json={"from": "preflight"})

    assert len(seen) == 2, f"两边都该经过代理，实际代理只看到 {seen}"


def test_a_profile_without_a_declared_contract_follows_the_llm_client_rule(proxy, monkeypatch):
    """llm_client 的隐含规则是 googleapis.com 强制走代理，preflight 要沿用同一条。"""
    url, seen = proxy
    monkeypatch.setenv("AUTORESEARCH_PROXY_URL", url)
    import preflight

    gemini = {"base_url": "https://generativelanguage.googleapis.com"}
    assert preflight.proxy_contract_of(gemini) == proxy_contract.AUTORESEARCH

    other = {"base_url": "https://api.example.com"}
    assert preflight.proxy_contract_of(other) == proxy_contract.DIRECT


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://generativelanguage.googleapis.com/v1", proxy_contract.AUTORESEARCH),
        ("https://evil.example/googleapis.com", proxy_contract.DIRECT),
        ("https://googleapis.com.evil.example/v1", proxy_contract.DIRECT),
    ],
)
def test_legacy_googleapis_inference_uses_the_parsed_hostname(base_url, expected):
    """A domain name in a path or a sibling hostname must not change routing."""
    import preflight

    assert proxy_contract.contract_for_legacy_base_url(base_url) == expected
    assert preflight.proxy_contract_of({"base_url": base_url}) == expected


@pytest.mark.parametrize(
    ("base_url", "expected_needs_proxy"),
    [
        ("https://generativelanguage.googleapis.com/v1", True),
        ("https://evil.example/googleapis.com", False),
        ("https://googleapis.com.evil.example/v1", False),
    ],
)
def test_legacy_consumer_uses_the_same_hostname_contract(
    monkeypatch, base_url, expected_needs_proxy
):
    """The preflight rule above is useful only if the real caller shares it."""
    import llm_client

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"}}]}

    seen = {}
    monkeypatch.setattr(
        llm_client,
        "get_preset",
        lambda _name: {"base_url": base_url, "api_key": "test", "model": "test"},
    )

    def capture(_url, **kwargs):
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr(llm_client, "_post_with_retries", capture)

    assert llm_client.call_chat_completions("probe", max_tokens=1) == "ok"
    assert seen["needs_proxy"] is expected_needs_proxy


def test_an_undeclared_vertex_profile_defaults_to_the_standard_variables():
    """llm_client 里没有任何 vertex 代码。

    仓里的 Vertex 消费者只有执行侧那半边：monitor 用 requests，运行时的
    client.ts 走 getProxyFetchOptions()，两者都读标准变量。把 auth=vertex 缺省成
    autoresearch，会让新加的 vertex profile 探一条没有消费者走的路。
    """
    import preflight

    assert preflight.proxy_contract_of({"auth": "vertex"}) == proxy_contract.STANDARD_ENV
    assert preflight.proxy_contract_of(
        {"auth": "vertex-or-api-key"}) == proxy_contract.STANDARD_ENV


# ---- 代理配了但不通：快速失败，且仍然算 transport ----

def test_an_unreachable_proxy_fails_fast_and_stays_a_transport_failure(monkeypatch):
    """等满 60 秒再报超时，是「看起来卡死」的一半原因。

    但它不能被重新分类成 config：请求已经具备构造条件，是网络路径不通。config 是
    「缺变量、类型不匹配」，两者混在一起会让刚分清的三态退回去。
    """
    import time

    import preflight
    with dead_proxy_url() as proxy_url:
        monkeypatch.setenv("AUTORESEARCH_PROXY_URL", proxy_url)

        profile = {"proxy_contract": "autoresearch", "base_url": "http://upstream.invalid"}
        started = time.time()
        result = preflight.proxy_unreachable(profile, TARGET, started)

    assert result is not None, "代理不通时不该继续往下探"
    assert result.stage == "transport", "到不了不是配置错"
    assert "端口不通" in result.detail
    assert time.time() - started < 5, "应该快速失败，不是等满超时"


def test_a_live_proxy_does_not_block_the_probe(proxy, monkeypatch):
    """反方向的负控：代理活着时不能顺手把探测拦下来。"""
    import time

    import preflight
    url, _ = proxy
    monkeypatch.setenv("AUTORESEARCH_PROXY_URL", url)

    profile = {"proxy_contract": "autoresearch", "base_url": "http://upstream.invalid"}
    assert preflight.proxy_unreachable(profile, TARGET, time.time()) is None


# ---- 配置里的声明要和消费者源码对得上 ----

CONSUMER_CONTRACTS = {
    # profile -> (契约, 消费者源码里能证明它的那段)
    "gemini-3.1-flash-lite": (proxy_contract.AUTORESEARCH, None),
    "gemini-3.1-pro": (proxy_contract.AUTORESEARCH, None),
    "gemini-pro": (proxy_contract.AUTORESEARCH, None),
}


@pytest.mark.parametrize("name", sorted(CONSUMER_CONTRACTS))
def test_the_declared_contract_matches_what_the_consumer_does(name):
    """配置里的 proxy_contract 是声明，消费者源码才是事实。

    providers.example.json 只有 preflight 会读，所以声明写错没有任何东西会报错，除了
    这一条：它去消费者源码里找那个决定代理行为的调用。
    """
    config = json.loads((REPO / "config" / "providers.example.json").read_text(encoding="utf-8"))
    profile = providers.as_profiles(config)[name]
    want, evidence = CONSUMER_CONTRACTS[name]

    assert profile.get("proxy_contract") == want, f"{name} 声明的契约和消费者对不上"
    if evidence:
        path, needle = evidence
        source = (REPO / path).read_text(encoding="utf-8", errors="ignore")
        assert needle in source, (
            f"{path} 里找不到 {needle!r}：消费者换了发请求的方式，{name} 的契约要重新核"
        )


# ---- 不需要代理的机器（RunPod / 境外 VPS）----

def test_a_host_with_no_proxy_at_all_probes_direct_on_both_sides(monkeypatch):
    """Pod 上没人设代理、7890 也没人听。preflight 和消费者都该直连。

    只改一边就会造出反向的分叉：preflight 报 transport 失败，而 llm_client 直连成功。
    这个仓库刚花一整轮消灭的就是这种分叉。
    """
    import importlib
    import time

    import llm_client
    import preflight

    monkeypatch.delenv("AUTORESEARCH_PROXY_URL", raising=False)
    monkeypatch.setattr(proxy_contract, "proxy_alive", lambda *a, **k: False)
    importlib.reload(llm_client)

    profile = {"proxy_contract": "autoresearch", "base_url": "http://upstream.invalid"}

    assert preflight.proxy_unreachable(profile, TARGET, time.time()) is None, \
        "没配代理的机器上不该报代理不通"
    consumer = llm_client._httpx_kwargs(needs_proxy=True, timeout=5)
    assert consumer is not None and "proxy" not in consumer, "消费者也该直连"


def test_an_explicitly_configured_dead_proxy_blocks_both_sides(monkeypatch):
    """受限网络上操作者指了代理而它死了：两边都要明确失败，不许偷偷直连。"""
    import importlib
    import time

    import llm_client
    import preflight

    with dead_proxy_url() as proxy_url:
        monkeypatch.setenv("AUTORESEARCH_PROXY_URL", proxy_url)
        importlib.reload(llm_client)

        profile = {"proxy_contract": "autoresearch", "base_url": "http://upstream.invalid"}
        blocked = preflight.proxy_unreachable(profile, TARGET, time.time())

        assert blocked is not None and blocked.stage == "transport"
        assert llm_client._httpx_kwargs(needs_proxy=True, timeout=5) is None


def test_a_dead_required_proxy_fails_fast_instead_of_waiting(monkeypatch):
    """代理死了就立刻回 transport 失败，不去等满超时。

    原来这条按源码字面断言「每个 probe 里出现过 proxy_unreachable」。把发送收敛进
    providers.send 之后那个字面消失了，而行为反而更强：判断只在一处，谁也绕不过去。
    按行为断言，改动实现不会误红，删掉那个判断才会。
    """
    import providers

    monkeypatch.setattr(providers.proxy_contract, "effective_kwargs", lambda *a, **kw: None)

    def explode(*a, **kw):
        raise AssertionError("代理不可用时不该真的发请求")

    monkeypatch.setattr(providers.httpx, "Client", explode)

    for dialect in providers.DIALECTS.values():
        reply = providers.call(dialect, {"base_url": "https://x", "api_key": "k"}, "m")
        assert reply.outcome == providers.UNREACHABLE
        assert reply.stage == "transport", f"{dialect.name} 该记成传输层失败"


def test_the_route_label_says_what_will_actually_happen(monkeypatch):
    """不受限主机上默认 7890 是死的、变量也没设，请求会直连。

    进度行如果照 proxy_url_for 打，会写成经由那个代理，和实际发生的事相反。
    这个分支的全部意义就是别再让人看错自己在探哪条路。
    """
    monkeypatch.delenv("AUTORESEARCH_PROXY_URL", raising=False)
    monkeypatch.setattr(proxy_contract, "proxy_alive", lambda *a, **k: False)

    assert proxy_contract.describe(proxy_contract.AUTORESEARCH, TARGET) == "直连"


def test_the_route_label_marks_an_explicitly_dead_proxy(proxy, monkeypatch):
    """显式指定的代理死了不会退回直连，标签要说清它不可用。"""
    with dead_proxy_url() as proxy_url:
        monkeypatch.setenv("AUTORESEARCH_PROXY_URL", proxy_url)

        label = proxy_contract.describe(proxy_contract.AUTORESEARCH, TARGET)

        assert "不可用" in label
