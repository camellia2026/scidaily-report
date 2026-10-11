"""可靠性小工具：错误分类 + 带退避的重试。

agent 系统里「调用一次就成功」是理想情况。工程上必须假设：

    - 网络会抖（连接失败 / 读超时）
    - 服务会限流（429）或短暂故障（5xx）
    - 调用可能挂住（所以要设超时，见 config 的 LLM_TIMEOUT_S）

重试三要素（本文件回答的就是这三个问题）：
    重试什么？ —— 只重试「瞬时错误」，参数写错这类致命错误重试一万次也没用
    重试几次？ —— 必须有上限（attempts），重试不是万能药
    隔多久再试？ —— 指数退避：0.5s, 1s, 2s… 给服务恢复的时间，也避免打垮它

两个注意点：
  1) OpenAI / Anthropic 的 SDK 本身对连接错误和 429/5xx 已自动重试两次左右；
     本模块的定位是「业务层再包一层」以及离线演示、mock 测试。
  2) 生产环境还应在退避里加随机抖动（jitter）避免多客户端共振；教学从简。

注意：重试的前提是「重试是安全的」。对会改变外部状态的操作（下单、发邮件、
扣款），重试可能造成重复副作用 —— 要么操作本身幂等，要么别自动重试。
"""
from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# 这些 HTTP 状态码通常代表「等一下再试就好了」
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class TransientError(Exception):
    """可重试的瞬时错误（网络抖动、限流、服务临时故障）。"""


def classify_error(exc: BaseException) -> str:
    """把异常分成 'transient'（值得重试）或 'fatal'（重试没用）。

    判定顺序：
      1) 我们自己的 TransientError
      2) 带 status_code 的 HTTP 异常（openai/anthropic SDK 的错误都带）
      3) 类名启发式（各 SDK 异常类名不统一时的兜底）
    """
    if isinstance(exc, TransientError):
        return "transient"

    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in TRANSIENT_STATUS:
        return "transient"

    name = type(exc).__name__
    if any(k in name for k in ("RateLimit", "Connection", "Timeout", "InternalServer", "ServiceUnavailable")):
        return "transient"

    return "fatal"


def retry_call(
    fn: Callable[..., T],
    *args: Any,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    **kwargs: Any,
) -> T:
    """调用 fn(*args, **kwargs)，瞬时错误自动重试。

    第 n 次失败后等待 min(base_delay * 2^(n-1), max_delay) 秒再试；
    attempts 次（含首次）都失败，或遇到致命错误，就抛出原异常。

    on_retry(第几次失败, 异常, 等待秒数) 用于观察/日志。
    """
    for i in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if classify_error(e) != "transient":
                raise                                # 致命错误：立即抛，别浪费时间
            if i == attempts:
                raise                                # 次数用完：把最后的错误如实抛出
            delay = min(base_delay * (2 ** (i - 1)), max_delay)
            if on_retry:
                on_retry(i, e, delay)
            time.sleep(delay)
    raise AssertionError("不可达")  # pragma: no cover —— 循环内必然 return 或 raise
