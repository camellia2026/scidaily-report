"""离线脚本化假模型（MockLLM）。

为什么课程需要"假模型"：
  1) 断网、没有 API key、或者本地模型没启动时，整条 agent 流程依然能跑通 ——
     课程的示例不应该因为环境问题变成"跑不起来"
  2) smoke test 需要确定性：脚本写死每次返回什么，判断"通过/失败"才有意义
  3) 把"模型"变成一个完全可控的部件后，你可以精确构造各种边缘场景
     （参数给错、工具报错、一直调工具不停……）来测试你的 agent 代码

脚本格式（一个列表，按顺序消费，每次 chat() 取一项）：
    字符串                                  -> 直接返回一段文本
    {"text": "...", "tool_calls": [...]}    -> 返回文本 + 工具调用（字段都可省）
    {"tool_calls": [{"name": "xx", "arguments": {...}}, ...]}  -> 只发起工具调用
    可调用对象(messages)                    -> 根据上下文动态决定（进阶用法）

示例：
    m = MockLLM([
        {"tool_calls": [{"name": "get_current_time", "arguments": {}}]},
        "现在是 15:30。",
    ])
"""
from __future__ import annotations

import json
import threading

from .llm import LLMResponse, ToolCall


class MockLLM:
    def __init__(self, script: list | None = None):
        self.script = list(script or [])
        self._pos = 0
        self._lock = threading.Lock()  # L04 并行模式会在多线程里调用，加锁保平安
        self.calls: list[list[dict]] = []  # 记录每次收到的 messages（教学观察用）

    def _take(self):
        """取脚本的下一项。返回 (item, 是否已看完)。"""
        with self._lock:
            if self._pos < len(self.script):
                item = self.script[self._pos]
                self._pos += 1
                return item, self._pos >= len(self.script)
            return None, True

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        self.calls.append(messages)
        item, _exhausted = self._take()

        if callable(item):
            item = item(messages)

        if item is None:
            # 脚本用完：给一个明确的默认回复，方便发现"脚本写少了"
            return LLMResponse(
                content="(mock) 脚本已用完，这是默认回复。",
                tool_calls=[], model="mock-llm",
                usage={"input_tokens": 0, "output_tokens": 0},
                stop_reason="end_turn",
            )

        if isinstance(item, str):
            text, calls_spec = item, []
        elif isinstance(item, dict):
            text = item.get("text", "")
            calls_spec = item.get("tool_calls") or []
        else:
            raise TypeError(f"MockLLM 脚本项必须是 str / dict / 可调用对象，收到 {type(item).__name__}")

        tool_calls = []
        for i, spec in enumerate(calls_spec, 1):
            args = spec.get("arguments", {})
            tool_calls.append(ToolCall(
                id=f"call_mock_{self._pos}_{i}",
                name=spec["name"],
                arguments=args,
                arguments_raw=json.dumps(args, ensure_ascii=False),
            ))

        return LLMResponse(
            content=text,
            tool_calls=tool_calls,
            model="mock-llm",
            usage={"input_tokens": 0, "output_tokens": 0},
            stop_reason="tool_use" if tool_calls else "end_turn",
        )

    def chat_stream(self, messages: list[dict]):
        """把脚本里的文本按 3 个字符一段吐出来，模拟流式效果。"""
        resp = self.chat(messages)
        text = resp.content or ""
        for i in range(0, len(text), 3):
            yield text[i:i + 3]
