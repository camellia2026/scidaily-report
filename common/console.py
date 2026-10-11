"""统一的控制台输出工具。

为什么需要这个文件？
-------------------
Windows 中文系统的控制台默认编码是 GBK(cp936)，直接打印某些字符
（emoji、特殊符号、生僻字）会抛 UnicodeEncodeError 让程序崩溃。
课程所有脚本都在开头调用 fix_console() 一次性把输出流切到 UTF-8。

两条课程约定：
  1. 代码输出只用 ASCII 标签：[LLM] [TOOL] [AGENT] [OK] [WARN] [ERR] [INFO]
     不用 emoji —— 这样即使在某台没修复成功的机器上也不会炸
  2. 所有文件读写都显式写 encoding="utf-8"

这本身就是"工程细节决定 agent 能不能稳定跑"的例子。你在 Claude Code
里看到的流畅输出，背后同样要处理这类平台差异。
"""
import sys


def fix_console() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 GBK 控制台打印非 ASCII 时崩溃。

    每个课程脚本的第一行（在 import common 之后）都应该调用它。
    被重定向或无 reconfigure 的环境下静默跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def log(tag: str, message: str) -> None:
    """统一日志格式：[TAG] message（立即 flush，便于观察 agent 的实时过程）"""
    print(f"[{tag}] {message}", flush=True)


def banner(title: str, width: int = 64) -> None:
    print("=" * width)
    print(title)
    print("=" * width)


def info(message: str) -> None:
    log("INFO", message)


def ok(message: str) -> None:
    log("OK", message)


def warn(message: str) -> None:
    log("WARN", message)


def err(message: str) -> None:
    log("ERR", message)
