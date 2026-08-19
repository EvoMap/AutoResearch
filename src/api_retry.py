"""统一的外部 API 重试策略。

调用方只描述什么结果值得重试，以及最多尝试几次。等待、指数退避和 HTTP
429/5xx 的判定集中在这里，避免每个模型或采集器各写一套循环。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import httpx

DEFAULT_ATTEMPTS = 3
MAX_DELAY_SECONDS = 30.0
TRANSIENT_HTTP_STATUSES = {408, 425, 429}

T = TypeVar("T")


@dataclass(frozen=True)
class AttemptFailure:
    attempt: int
    kind: str
    detail: str


class RetryError(RuntimeError):
    """所有允许的尝试都因异常失败。"""

    def __init__(self, operation: str, failures: list[AttemptFailure]):
        self.operation = operation
        self.failures = tuple(failures)
        detail = "；".join(
            f"第 {failure.attempt} 次 {failure.kind}: {failure.detail}"
            for failure in failures
        )
        super().__init__(f"{operation} 在 {len(failures)} 次尝试后失败：{detail}")


def exponential_delay(attempt: int, base: float = 1.0,
                      ceiling: float = MAX_DELAY_SECONDS) -> float:
    """第 `attempt` 次失败后应等待的秒数；attempt 从 1 开始。"""
    return min(base * (2 ** max(0, attempt - 1)), ceiling)


def retry_call(
    operation: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    should_retry_result: Callable[[T], bool] | None = None,
    should_retry_exception: Callable[[Exception], bool] | None = None,
    delay_for: Callable[[int, T | None, Exception | None], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
    on_attempt: Callable[[int, T | None, Exception | None], Any] | None = None,
    operation_name: str = "API 调用",
) -> T:
    """执行一个调用，并只对调用方声明的暂时性失败重试。

    `attempts` 是总尝试次数。结果型失败在次数耗尽后返回最后一个结果，异常型
    失败则抛出带完整历史的 `RetryError`。
    """
    if attempts < 1:
        raise ValueError("attempts 必须至少为 1")

    retry_result = should_retry_result or (lambda _result: False)
    retry_exception = should_retry_exception or (lambda _error: True)
    wait = sleep or time.sleep
    failures: list[AttemptFailure] = []

    for attempt in range(1, attempts + 1):
        try:
            result = operation()
        except Exception as exc:
            if on_attempt:
                on_attempt(attempt, None, exc)
            if not retry_exception(exc):
                raise
            failures.append(AttemptFailure(attempt, type(exc).__name__, str(exc)))
            if attempt == attempts:
                raise RetryError(operation_name, failures) from exc
            delay = (delay_for(attempt, None, exc) if delay_for
                     else exponential_delay(attempt))
            wait(max(0.0, delay))
            continue

        if on_attempt:
            on_attempt(attempt, result, None)
        if not retry_result(result) or attempt == attempts:
            return result
        delay = (delay_for(attempt, result, None) if delay_for
                 else exponential_delay(attempt))
        wait(max(0.0, delay))

    raise AssertionError("retry loop ended without a result")


def is_transient_http(response: httpx.Response) -> bool:
    return (response.status_code in TRANSIENT_HTTP_STATUSES
            or response.status_code >= 500)


def http_delay(attempt: int, response: httpx.Response | None,
               _error: Exception | None, *, base: float = 1.0) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after", "").strip()
        if retry_after.isdigit():
            return min(float(retry_after), MAX_DELAY_SECONDS)
    return exponential_delay(attempt, base)


def retry_http(operation: Callable[[], httpx.Response], *,
               attempts: int = DEFAULT_ATTEMPTS, retry_delay: float = 1.0,
               sleep: Callable[[float], Any] | None = None,
               operation_name: str = "HTTP 请求") -> httpx.Response:
    return retry_call(
        operation,
        attempts=attempts,
        should_retry_result=is_transient_http,
        should_retry_exception=lambda error: isinstance(error, httpx.TransportError),
        delay_for=lambda attempt, response, error: http_delay(
            attempt, response, error, base=retry_delay),
        sleep=sleep,
        operation_name=operation_name,
    )


def http_get(url: str, *, attempts: int = DEFAULT_ATTEMPTS,
             retry_delay: float = 1.0, **kwargs) -> httpx.Response:
    return retry_http(
        lambda: httpx.get(url, **kwargs),
        attempts=attempts,
        retry_delay=retry_delay,
        operation_name=f"GET {url}",
    )
