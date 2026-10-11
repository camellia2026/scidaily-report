"""配置加载：.env -> Config 对象。

课程设计约定：所有脚本从不硬编码"用哪个模型"，而是统一经过 get_config()。
改一行 .env 里的 LLM_PROVIDER 就能整体切换后端 —— 这是课程
"provider 无关"设计的最小实现，也是 L01 讲适配层时的地基。
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 课程根目录 = 本文件所在目录（common/）的上一级
ROOT = Path(__file__).resolve().parents[1]


class ConfigError(Exception):
    """配置有问题时抛出，message 里会带明确的修复指引。"""


@dataclass
class Config:
    provider: str            # ollama | deepseek | anthropic | mock
    model: str
    base_url: str | None
    api_key: str | None
    max_steps: int           # agent 循环上限（安全阀）
    temperature: float
    anthropic_max_tokens: int
    reasoning_effort: str | None = None  # "none"=关闭思考（Ollama 思考型模型）
    timeout_s: float | None = None       # 单次请求超时（秒）；None=用 SDK 默认

    def describe(self) -> str:
        key_state = "已配置" if self.api_key and self.api_key != "ollama" else "无需 key"
        return (f"provider={self.provider}  model={self.model}  "
                f"base_url={self.base_url or '(官方默认)'}  api_key={key_state}")


def get_config(provider: str | None = None) -> Config:
    """读取 .env 并生成 Config。

    参数 provider 优先级高于 .env 里的 LLM_PROVIDER（脚本 --provider 参数用）。
    已存在的环境变量不会被 .env 覆盖（load_dotenv 的默认行为），
    所以临时切换也可以直接在命令行 set LLM_PROVIDER=xxx。
    """
    load_dotenv(ROOT / ".env")

    name = (provider or os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
    max_steps = int(os.getenv("MAX_STEPS") or 8)
    temperature = float(os.getenv("TEMPERATURE") or 0.7)
    anthropic_max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS") or 2048)
    # 单次请求超时（秒）。留空 = 用 SDK 默认。宁可超时报错，不要无声挂死。
    _timeout_raw = (os.getenv("LLM_TIMEOUT_S") or "").strip()
    timeout_s = float(_timeout_raw) if _timeout_raw else None

    if name == "ollama":
        # qwen3.5 这类"思考型"模型默认会先长篇思考再回答（可能几十秒、上千 token）。
        # reasoning_effort="none" 关闭思考（快很多）；设 "low"/"medium"/"high" 或留空则用模型默认。
        effort = (os.getenv("OLLAMA_REASONING_EFFORT") or "").strip().lower() or None
        return Config(
            provider=name,
            model=os.getenv("OLLAMA_MODEL") or "qwen3.5:4b-fast",
            base_url=(os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434/v1").rstrip("/"),
            api_key="ollama",  # OpenAI SDK 要求非空；Ollama 不校验
            max_steps=max_steps,
            temperature=temperature,
            anthropic_max_tokens=anthropic_max_tokens,
            reasoning_effort=effort,
            timeout_s=timeout_s,
        )

    if name == "deepseek":
        key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
        if not key:
            raise ConfigError(
                "使用 deepseek 需要 DEEPSEEK_API_KEY。请把它填进 .env "
                "（可从你本机已有的 DeepSeek 服务配置复制，注意不要提交 git），"
                "或改回 LLM_PROVIDER=ollama / mock。"
            )
        return Config(
            provider=name,
            model=os.getenv("DEEPSEEK_MODEL") or "deepseek-flash",
            base_url=(os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/"),
            api_key=key,
            max_steps=max_steps,
            temperature=temperature,
            anthropic_max_tokens=anthropic_max_tokens,
            timeout_s=timeout_s,
        )

    if name == "anthropic":
        key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise ConfigError(
                "使用 anthropic 需要 ANTHROPIC_API_KEY（在 .env 中填写）。"
                "此分支仅用于协议对照教学，日常学习用 ollama / mock 即可。"
            )
        return Config(
            provider=name,
            model=os.getenv("ANTHROPIC_MODEL") or "claude-opus-4-8",
            base_url=os.getenv("ANTHROPIC_BASE_URL") or None,
            api_key=key,
            max_steps=max_steps,
            temperature=temperature,
            anthropic_max_tokens=anthropic_max_tokens,
            timeout_s=timeout_s,
        )

    if name == "mock":
        return Config(
            provider=name,
            model="mock-llm",
            base_url=None,
            api_key=None,
            max_steps=max_steps,
            temperature=temperature,
            anthropic_max_tokens=anthropic_max_tokens,
            timeout_s=timeout_s,
        )

    raise ConfigError(
        f"未知的 LLM_PROVIDER: {name!r}（可选：ollama / deepseek / anthropic / mock）"
    )
