"""消息格式与协议适配：理解 OpenAI 与 Anthropic 两套报文的差异。

本文件是课程的"核心教具"之一。核心洞察：
    不同厂商的 LLM API 说的其实是"同一件事的两种方言"——
    用户说了什么、模型回了什么、模型要调哪个工具、工具返回了什么。
本课程内部统一用 OpenAI 风格的消息 dict 作为"普通话"（大多数国内
服务商也兼容它），在需要时翻译成 Anthropic 方言。这层翻译（适配层）
是所有多模型 agent 框架里最不起眼、但最核心的代码。

两套格式的关键差异（对照阅读 L02 笔记的报文示例）：
  1. system 提示：OpenAI 作为一条 messages 消息；Anthropic 是单独的 system 参数
  2. 工具调用：OpenAI 用 assistant.tool_calls 字段 + role="tool" 消息回结果；
     Anthropic 用 content 块（tool_use / tool_result）
  3. 角色交替：Anthropic 要求 user/assistant 严格交替，
     连续的 tool 结果必须合并成同一条 user 消息里的多个 tool_result 块
  4. 工具定义：OpenAI 是 {"type":"function","function":{...parameters...}}；
     Anthropic 是 {"name","description","input_schema"}
"""
import json


# ---------- 构造消息的小助手（比手写 dict 更不容易写错字段名） ----------

def system(text: str) -> dict:
    return {"role": "system", "content": text}


def user(text: str) -> dict:
    return {"role": "user", "content": text}


def assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def assistant_with_tool_calls(tool_calls, content: str | None = None) -> dict:
    """构造"模型要求调用工具"的 assistant 消息（OpenAI 格式）。

    tool_calls 是 common.llm.ToolCall 对象列表。这条消息必须记进历史，
    否则下一轮模型看不到自己刚才要求过什么。
    """
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.name,
                "arguments": tc.arguments_raw
                             or json.dumps(tc.arguments, ensure_ascii=False),
            },
        } for tc in tool_calls],
    }


def tool_result(tool_call_id: str, content: str) -> dict:
    """OpenAI 风格的工具结果消息（role="tool"）。"""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


# ---------- 解析工具参数 ----------

def parse_tool_arguments(raw: str) -> tuple[dict, str | None]:
    """解析模型返回的工具参数 JSON。

    小模型（尤其是本地小模型）经常返回非法 JSON。约定：
    解析失败不抛异常，而是返回 {"_parse_error": ...}，
    由下游把错误信息回传给模型让它重试 —— 这是可靠性的第一道防线。
    返回 (参数 dict, 错误说明或 None)。
    """
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return ({"_parse_error": f"参数不是合法 JSON：{e}"}, str(e))
    if not isinstance(data, dict):
        return ({"_parse_error": f"参数应该是 JSON 对象，实际是 {type(data).__name__}"},
                "参数不是 JSON 对象")
    return data, None


# ---------- OpenAI 格式 -> Anthropic 格式 ----------

def to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """把课程内部（OpenAI 风格）的消息列表翻译成 Anthropic 格式。

    返回 (system 文本, anthropic messages)。
    """
    system_parts: list[str] = []
    out: list[dict] = []

    for m in messages:
        role = m["role"]

        if role == "system":
            system_parts.append(m.get("content") or "")
            continue

        if role in ("user", "assistant"):
            blocks: list[dict] = []
            text = m.get("content")
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                args, _err = parse_tool_arguments(fn.get("arguments") or "{}")
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": fn["name"],
                    "input": args,
                })
            _append_merged(out, role, blocks)
            continue

        if role == "tool":
            # Anthropic 里工具结果放在 user 消息的 tool_result 块中
            _append_merged(out, "user", [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": m.get("content") or "",
            }])
            continue

        raise ValueError(f"未知的消息 role: {role!r}")

    return "\n\n".join(p for p in system_parts if p), out


def _append_merged(out: list[dict], role: str, blocks: list[dict]) -> None:
    """Anthropic 要求 user/assistant 严格交替：把相邻同角色的消息合并成一条。

    典型场景：模型一次要求调用两个工具，OpenAI 格式里是两条 role="tool"
    消息，翻译到 Anthropic 必须合并成一条 user 消息里的两个 tool_result 块。
    """
    if out and out[-1]["role"] == role:
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": blocks})


def to_anthropic_tools(openai_tools: list[dict]) -> list[dict]:
    """OpenAI 格式的工具说明 -> Anthropic 格式。"""
    return [{
        "name": t["function"]["name"],
        "description": t["function"].get("description", ""),
        "input_schema": t["function"].get("parameters")
                        or {"type": "object", "properties": {}},
    } for t in openai_tools]


def to_openai_tools(anthropic_tools: list[dict]) -> list[dict]:
    """Anthropic 格式的工具说明 -> OpenAI 格式（反向翻译）。"""
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema")
                          or {"type": "object", "properties": {}},
        },
    } for t in anthropic_tools]


# ---------- 打印用 ----------

def format_transcript(messages: list[dict]) -> str:
    """把消息列表排版成可读的对话轨迹（教学演示/调试用）。"""
    lines: list[str] = []
    for m in messages:
        role = m["role"]
        if role == "assistant" and m.get("tool_calls"):
            if m.get("content"):
                lines.append(f"[assistant] {m['content']}")
            for tc in m["tool_calls"]:
                fn = tc["function"]
                lines.append(f"[assistant -> tool] {fn['name']}({fn.get('arguments')})")
        elif role == "tool":
            lines.append(f"[tool 回填] {m.get('content', '')}")
        else:
            lines.append(f"[{role}] {m.get('content') or ''}")
    return "\n".join(lines)
