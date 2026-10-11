"""结构化输出管线：把模型的自由文本变成程序可信的数据。

本课的核心模型 —— 四层防线：

    约束（尽力而为） → 解析（必须） → 校验（必须） → 修复（兜底）
    response_format      extract_json    pydantic      把错误喂回模型重试
    / format / schema

为什么不能只靠 json.loads：
    模型的输出默认是「文本」而不是「数据」—— 可能带代码围栏、前后客套话、
    甚至语法错误。json.loads 只应该用在「你完全控制生成过程」的字符串上。
    本机实测（2026-09，ollama 0.24.0 + qwen3.5）：即使开了 JSON mode，
    输出仍常常带 ```json 围栏，裸 json.loads 直接抛异常。

为什么校验和修复要分开：
    解析只管「是不是 JSON」，校验管「是不是我们要的数据」。
    一个语法完全合法的 JSON 照样可能字段缺失、类型不对、日期格式错。
    校验失败的详细信息（pydantic 给的那串）恰好是让模型自我修复的最好提示。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError


class JSONExtractionError(ValueError):
    """在文本里找不到可解析的 JSON。"""


# ---------------------------------------------------------------------------
# 解析层：从任意文本里把 JSON 抠出来
# ---------------------------------------------------------------------------

def _balanced_slice(text: str, start: int) -> str | None:
    """从 text[start]（{ 或 [）开始找配平的片段。

    难点在「字符串里的括号不算数」：{"note": "用 { } 表示集合"} 里，
    花括号出现在字符串内部，正则和简单的 find 都会数错。
    所以要逐字符扫描，并跟踪「是否在字符串里 / 是否处于转义」两个状态。
    """
    pairs = {"{": "}", "[": "]"}
    stack: list[str] = []
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in pairs:
            stack.append(pairs[ch])
        elif ch in "}]":
            if not stack or ch != stack[-1]:
                return None          # 括号种类对不上，从这里开始的片段无效
            stack.pop()
            if not stack:
                return text[start:i + 1]
    return None


def extract_json(text: str) -> Any:
    """尽力从模型输出里提取第一个 JSON 值。

    三级策略（从便宜到贵）：
        1) 整个文本就是 JSON                -> json.loads
        2) ```json 代码围栏包裹（最常见）    -> 剥掉围栏再 loads
        3) 前后有客套话/解释                 -> 扫描第一个配平的 {...} 或 [...]

    注意：本函数只负责「抠出来」，不负责「修语法」。
    单引号 JSON、多余逗号这类语法错误不在修复范围 —— 那是修复层
    （把错误喂回模型重试）的工作。解析层越简单可靠，调试越容易。
    """
    if not text or not text.strip():
        raise JSONExtractionError("输出为空")

    s = text.strip()

    # 1) 整体就是 JSON
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 2) 代码围栏（```json ... ``` / ``` ... ```）
    import re
    m = re.search(r"```[ \t]*[A-Za-z]*[ \t]*\r?\n(.*?)```", s, re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3) 扫描：逐个尝试 { 或 [ 开头，找配平且能解析的片段
    for opener in ("{", "["):
        idx = s.find(opener)
        while idx != -1:
            candidate = _balanced_slice(s, idx)
            if candidate is not None:
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass
            idx = s.find(opener, idx + 1)

    raise JSONExtractionError(
        f"输出里没有可解析的 JSON（前 80 字符：{s[:80]!r}）"
    )


# ---------------------------------------------------------------------------
# 校验层：把校验错误整理成「模型看得懂的修复提示」
# ---------------------------------------------------------------------------

def format_validation_error(err: ValidationError, *, max_value_len: int = 60) -> str:
    """把 pydantic 的校验错误压缩成几行短消息。

    直接 str(err) 是多行英文长文，小模型看了容易抓不住重点。
    压缩成「- 字段: 问题（收到: xxx）」的形式，修复提示的命中率明显更高。
    """
    lines = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "(根)"
        msg = e["msg"]
        inp = e.get("input")
        shown = repr(inp)
        if len(shown) > max_value_len:
            shown = shown[:max_value_len] + "…"
        lines.append(f"- {loc}: {msg}（收到: {shown}）")
    return "\n".join(lines)


def fields_hint(model_cls: type[BaseModel]) -> str:
    """生成「字段名: 类型」清单，拼进提示词用。"""
    def type_name(t: Any) -> str:
        if hasattr(t, "__name__"):
            return t.__name__
        return str(t).replace("typing.", "")

    return ", ".join(
        f"{name}: {type_name(f.annotation)}" for name, f in model_cls.model_fields.items()
    )


# ---------------------------------------------------------------------------
# 修复层：调用 -> 解析 -> 校验 ->（失败则带错误重试）
# ---------------------------------------------------------------------------

@dataclass
class StructuredResult:
    """一次成功的结构化调用。"""
    value: BaseModel
    attempts: int                                  # 实际调用模型的次数（1 = 一次通过）
    raw: str                                       # 最后一次的原始输出（教学观察用）
    repairs: list[str] = field(default_factory=list)  # 每次喂回模型的修复提示


class StructuredError(RuntimeError):
    """重试若干次后仍拿不到合法数据。异常里带着最后的原始输出，方便排查。"""

    def __init__(self, message: str, *, attempts: int, last_raw: str, last_error: str):
        super().__init__(message)
        self.attempts = attempts
        self.last_raw = last_raw
        self.last_error = last_error


def call_structured(
    client: Any,
    messages: list[dict],
    model_cls: type[BaseModel],
    *,
    attempts: int = 3,
    temperature: float = 0.0,
    response_format: dict | None = None,
    on_attempt: Callable[[int, str], None] | None = None,
    on_repair: Callable[[int, str], None] | None = None,
) -> StructuredResult:
    """带解析 + 校验 + 修复循环的结构化调用。

    参数：
        client        LLMClient（或任何有 .chat(messages, temperature=, response_format=) 的对象）
        messages      对话消息（会被复制，不会改动调用方的列表）
        model_cls     pydantic 模型：既是校验依据，也是「允许模型有多少自由」的设计决策
        attempts      最多调用几次（含首次）—— 修复循环必须有上限
        temperature   结构化任务默认 0.0：要的是稳定复现，不是创意
        response_format  可选的约束层（{"type": "json_object"} / json_schema）。
                      各家支持度不一，本机实测可能被直接忽略 —— 它只是优化，不是保证

    失败语义：attempts 次后仍失败 -> 抛 StructuredError（带最后的原文）。
    绝不返回「半个对象」—— 要么是可校验的数据，要么是明确的异常。
    """
    msgs = list(messages)
    repairs: list[str] = []
    last_raw = ""
    last_error = ""

    for i in range(1, attempts + 1):
        resp = client.chat(msgs, temperature=temperature, response_format=response_format)
        raw = resp.content or ""
        last_raw = raw
        if on_attempt:
            on_attempt(i, raw)

        # ---- 解析 ----
        try:
            data = extract_json(raw)
        except JSONExtractionError as e:
            last_error = str(e)
            feedback = (
                f"你的输出无法解析为 JSON：{last_error}\n"
                "请只输出一个 JSON 对象：不要代码围栏（```）、不要解释文字，"
                "字符串必须用双引号。"
            )
        else:
            # ---- 校验 ----
            try:
                value = model_cls.model_validate(data)
            except ValidationError as e:
                last_error = format_validation_error(e)
                feedback = (
                    "你的 JSON 语法没问题，但不符合要求的数据结构：\n"
                    f"{last_error}\n"
                    "请修正上述字段后，重新输出完整的 JSON 对象。"
                )
            else:
                # 解析 + 校验都过了
                return StructuredResult(value=value, attempts=i, raw=raw, repairs=repairs)

        # ---- 修复：把错误摘要作为下一轮的输入 ----
        repairs.append(feedback)
        if on_repair:
            on_repair(i, feedback)
        if i < attempts:
            msgs.append({"role": "assistant", "content": raw or "(空输出)"})
            msgs.append({"role": "user", "content": feedback})

    raise StructuredError(
        f"重试 {attempts} 次仍未通过校验（最后一次错误：{last_error}）",
        attempts=attempts,
        last_raw=last_raw,
        last_error=last_error,
    )
