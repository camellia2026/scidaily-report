"""LLMClient：把三种后端统一成同一个 chat() 接口。

设计目标（也是本课核心知识点）：
  上层 agent 代码只认识一种消息格式（OpenAI 风格的 messages 列表）
  和一种返回值（LLMResponse）。协议差异全部收敛在这一层：

      openai_compat   Ollama / DeepSeek / 任何 OpenAI 兼容端点（/v1）
      anthropic       Anthropic 原生 messages API
      mock            离线脚本化假模型（无网无 key 也能演示）

切换后端：.env 里改 LLM_PROVIDER，代码一行不用动。

为什么 stop_reason 要翻译成统一词表：
  OpenAI 的 finish_reason 是 "stop" / "tool_calls" / "length"，
  Anthropic 的 stop_reason 是 "end_turn" / "tool_use" / "max_tokens"，
  同一个意思两套词。课程内部统一成 end_turn / tool_use / length，
  agent 循环才不用为每家再写一套判断。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

from .config import Config, get_config
from .messages import parse_tool_arguments, to_anthropic_messages, to_anthropic_tools


@dataclass
class ToolCall:
    """模型发起的一次工具调用请求（已统一两种协议）。"""
    id: str                  # 调用标识：工具结果要带着它回填，模型才能对上号
    name: str
    arguments: dict          # 已解析的参数
    arguments_raw: str = ""  # 原始 JSON 字符串（调试/教学时看模型到底发了什么）


@dataclass
class LLMResponse:
    """统一后的模型返回。"""
    content: str                 # 文本内容（可能为空串）
    tool_calls: list[ToolCall]   # 结构化工具调用请求
    model: str
    usage: dict                  # {"input_tokens": int, "output_tokens": int}
    stop_reason: str             # 统一词表：end_turn | tool_use | length | 其他
    raw: Any = None              # 原始响应对象（教学时打印完整报文用）


class LLMError(RuntimeError):
    """后端返回了无法解析的响应。

    ★ 这是本项目对学习区 common/ 的**唯一一处偏离**（2026-09-25）。
      原因见 _chat_openai_compat 里响应守卫的注释。
    """


class LLMClient:
    def __init__(self, config: Config | None = None, mock_script: list | None = None):
        """mock_script 只在 provider == "mock" 时生效（离线脚本，见 mock_llm.py）。"""
        self.config = config or get_config()
        self.provider = self.config.provider
        self._mock = None
        self._client = None

        if self.provider == "mock":
            from .mock_llm import MockLLM
            self._mock = MockLLM(mock_script)
        elif self.provider in ("ollama", "deepseek"):
            # 两者都走 OpenAI 兼容协议：同一段代码，只换 base_url / key / model
            from openai import OpenAI
            kwargs: dict[str, Any] = {
                "base_url": self.config.base_url,
                "api_key": self.config.api_key or "not-needed",
            }
            if self.config.timeout_s:
                # 超时是"可靠性四件套"里最便宜的一件：宁可报错重试，不要无声挂死
                kwargs["timeout"] = self.config.timeout_s
            self._client = OpenAI(**kwargs)
        elif self.provider == "anthropic":
            from anthropic import Anthropic
            kwargs = {
                "api_key": self.config.api_key,
                "base_url": self.config.base_url or None,
            }
            if self.config.timeout_s:
                kwargs["timeout"] = self.config.timeout_s
            self._client = Anthropic(**kwargs)
        else:
            raise ValueError(f"未知 provider: {self.provider}")

    @property
    def mock_calls(self) -> int:
        """mock 后端实际被调用了几次（其它后端恒为 0）。

        L10 起使用：图/循环的调用点一旦比剧本多或少，mock 会安静地给出默认回复，
        冒烟照样绿、教学却是错的。用这个计数做断言，才能把「静默错位」揪出来。
        """
        return len(self._mock.calls) if self._mock else 0

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float | None = None,
             response_format: dict | None = None) -> LLMResponse:
        """发一次对话请求。tools 是 OpenAI 格式的工具说明（由 ToolRegistry 生成）。

        response_format（L05 起）：OpenAI 风格的输出约束，例如
            {"type": "json_object"}
            {"type": "json_schema", "json_schema": {"name": "...", "schema": {...}}}
        注意：它是「约束层」——各家支持度不一，本机 Ollama 兼容端点实测直接忽略。
        结构化输出的可靠性靠 structured.py 的解析+校验+修复管线，不靠这个参数。
        """
        if self.provider == "mock":
            return self._mock.chat(messages, tools)  # mock 忽略 response_format（剧本写死）
        if self.provider in ("ollama", "deepseek"):
            return self._chat_openai_compat(messages, tools, temperature, response_format)
        return self._chat_anthropic(messages, tools, temperature, response_format)

    def chat_stream(self, messages: list[dict],
                    temperature: float | None = None) -> Iterator[str]:
        """只做"文本流式输出"（增量吐出回复文字）。

        教学简化：流式 + 工具调用混合的场景很多厂商实现不一致，
        本课程只在纯聊天（L01 chat_cli）里用流式，工具类课程用 chat()。
        """
        if self.provider == "mock":
            yield from self._mock.chat_stream(messages)
            return

        if self.provider in ("ollama", "deepseek"):
            temp = temperature if temperature is not None else self.config.temperature
            stream = self._client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=temp,
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
            return

        # anthropic：流式协议细节与 OpenAI 差异较大，教学上不展开，一次性返回
        resp = self.chat(messages, temperature=temperature)
        if resp.content:
            yield resp.content

    # ------------------------------------------------------------------
    # 后端实现 1：OpenAI 兼容（Ollama / DeepSeek / 其他）
    # ------------------------------------------------------------------

    def _chat_openai_compat(self, messages, tools, temperature,
                            response_format=None) -> LLMResponse:
        temp = temperature if temperature is not None else self.config.temperature
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temp,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"  # 让模型自己决定要不要调工具
        if response_format:
            # 原样透传：OpenAI / DeepSeek 等会真的用它约束输出；
            # Ollama 兼容端点只是收下（实测 0.24.0 忽略它），不会报错
            kwargs["response_format"] = response_format
        if self.config.reasoning_effort:
            # 关掉/调整思考型模型的"思考"开销（仅 Ollama 支持此字段）
            kwargs["extra_body"] = {"reasoning_effort": self.config.reasoning_effort}

        resp = self._client.chat.completions.create(**kwargs)

        # ★ 响应守卫（本项目对学习区 common/ 的唯一一处偏离，2026-09-25）
        #
        # 实测踩到的坑：GitHub Models 返回了一个非对象的响应体，
        # 而 openai SDK 在非严格校验模式下走 construct_type()，**不做校验**，
        # 会把这个值原样返回（见 openai/_base_client.py 的 _process_response_data）。
        # 于是下面这行会炸成：
        #     AttributeError: 'str' object has no attribute 'choices'
        # 这个报错完全不告诉你后端到底返回了什么，整条降级链就断在这里。
        #
        # 守卫把它换成一句能直接定位问题的话（含原始响应体前 500 字符）。
        if not getattr(resp, "choices", None):
            raise LLMError(
                f"后端没有返回任何候选（响应类型 {type(resp).__name__}）。\n"
                f"  请求：model={self.config.model!r} base_url={self.config.base_url!r}\n"
                f"  原始响应前 500 字符：{str(resp)[:500]!r}"
            )

        choice = resp.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            raw = tc.function.arguments or "{}"
            args, _err = parse_tool_arguments(raw)
            tool_calls.append(ToolCall(
                id=tc.id, name=tc.function.name,
                arguments=args, arguments_raw=raw,
            ))

        usage = {"input_tokens": 0, "output_tokens": 0}
        if getattr(resp, "usage", None):
            usage = {
                "input_tokens": resp.usage.prompt_tokens or 0,
                "output_tokens": resp.usage.completion_tokens or 0,
            }

        # finish_reason -> 统一词表
        finish_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "length"}
        stop_reason = finish_map.get(choice.finish_reason or "", choice.finish_reason or "")
        if tool_calls and stop_reason == "end_turn":
            # 个别兼容端点 finish_reason 给得不规范，以"确实有 tool_calls"为准
            stop_reason = "tool_use"

        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            model=getattr(resp, "model", None) or self.config.model,
            usage=usage,
            stop_reason=stop_reason,
            raw=resp,
        )

    # ------------------------------------------------------------------
    # 后端实现 2：Anthropic 原生协议
    # ------------------------------------------------------------------

    def _chat_anthropic(self, messages, tools, temperature,
                        response_format=None) -> LLMResponse:
        system_text, anthropic_messages = to_anthropic_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.config.model,
            # Anthropic 协议要求显式 max_tokens（OpenAI 系则不需要）
            "max_tokens": self.config.anthropic_max_tokens,
            "messages": anthropic_messages,
        }
        # 注意：此处故意不传 temperature —— Opus 4.7+ / Fable 5 已移除采样参数，
        # 传了会直接 400。这也是"协议差异"的一部分：同一个概念各家支持度不同。
        if system_text:
            kwargs["system"] = system_text          # system 是单独参数，不在 messages 里
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools)
        if response_format:
            fmt = _anthropic_output_format(response_format)
            if fmt:
                # Anthropic 的结构化输出走 output_config.format（json_schema 形式）
                kwargs["output_config"] = {"format": fmt}

        resp = self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=dict(block.input or {}),
                    arguments_raw=json.dumps(block.input or {}, ensure_ascii=False),
                ))

        stop_map = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "length"}
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            model=resp.model,
            usage={
                "input_tokens": resp.usage.input_tokens or 0,
                "output_tokens": resp.usage.output_tokens or 0,
            },
            stop_reason=stop_map.get(resp.stop_reason or "", resp.stop_reason or ""),
            raw=resp,
        )


def _anthropic_output_format(response_format: dict) -> dict | None:
    """OpenAI 风格 response_format -> Anthropic output_config.format 的映射。

    Anthropic 没有 "json_object" 这种自由 JSON 模式，结构化输出只认
    json_schema（output_config.format）。所以 json_object 在这里映射为 None
    （忽略该参数）—— 反正解析+校验+修复管线会兜住。
    """
    if response_format.get("type") == "json_schema":
        spec = response_format.get("json_schema") or {}
        schema = spec.get("schema")
        if schema:
            return {"type": "json_schema", "schema": schema}
    return None


def make_client(provider: str | None = None,
                mock_script: list | None = None) -> LLMClient:
    """便捷入口：按 provider（默认读 .env）创建客户端。"""
    return LLMClient(get_config(provider), mock_script=mock_script)
