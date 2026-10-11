"""LLM 调用与结构化输出。

沿用原项目的骨架（call_structured + 降级链 + 规则兜底），换两处：
  1. 提示词变成**中英双语**——英文原标题原样保留，便于回原文核对；
  2. 分类集换成四个学科分区。

★ 一个刻意的设计决定：**不让模型决定条目属于哪一节。**
  「要闻 / 顶刊论文 / 热文 / 预印本 / 版本升级」这五节由候选条目来自哪个
  通道决定，是已知事实。让模型输出 section 只会引入一整类可避免的错误
  （把预印本归到顶刊论文里）。所以 DigestItem 里根本没有 section 字段，
  渲染阶段用 ref -> 通道 的映射补上。

  模型仍然负责 discipline，因为新闻稿没有期刊元数据可查。
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from common.config import Config, ConfigError, get_config
from common.console import warn
from common.llm import LLMClient
from common.reliability import classify_error
from common.structured import StructuredError, call_structured

from .filter import Candidate
from .settings import Settings

_DISCIPLINES = ("综合", "医学", "计算机", "数学与计算数学")
Discipline = Literal["综合", "医学", "计算机", "数学与计算数学"]

# 模型偶尔会给出近义说法，先归一再校验
_DISCIPLINE_ALIASES = {
    "综合": "综合", "其他": "综合", "交叉": "综合", "general": "综合",
    "医学": "医学", "生物医学": "医学", "临床": "医学", "medicine": "医学",
    "计算机": "计算机", "计算机科学": "计算机", "人工智能": "计算机",
    "机器学习": "计算机", "cs": "计算机", "computer science": "计算机",
    "machine learning": "计算机", "ai": "计算机",
    "数学": "数学与计算数学", "计算数学": "数学与计算数学",
    "数学与计算数学": "数学与计算数学", "numerics": "数学与计算数学",
}

NO_ABSTRACT = "(该源未提供摘要)"

# ★ pydantic 字段的长度上限，必须与上面 Field(max_length=...) 保持一致。
#   为什么要把它们提成常量：截断发生在两个地方（这里的规则兜底、
#   以及配置里的 report.max_title_chars），两处各截各的就会对不上——
#   实测踩到过：config 里 max_title_chars=90（英文标题需要更长），
#   而 headline_zh 的 max_length=60，于是规则兜底产出 90 字符直接崩在
#   pydantic 校验上。这与原项目 `_clip(text,60)` 没给省略号留位是同一类错误：
#   **截断长度和字段约束必须是同一个事实。**
_LIMIT_TITLE_EN = 300
_LIMIT_HEADLINE_ZH = 60
_LIMIT_SUMMARY_ZH = 320
_LIMIT_WHY_ZH = 200


class DigestItem(BaseModel):
    refs: list[int] = Field(description="这条摘要依据的候选编号，必须是给定列表里的", min_length=1)
    title_en: str = Field(
        description="论文的英文原标题，**必须逐字照抄候选行里的原标题，不得翻译或改写**",
        max_length=300,
    )
    headline_zh: str = Field(description="中文标题，不超过 40 字", max_length=60)
    summary_zh: str = Field(description="中文摘要，2-3 句，不超过 200 字", max_length=320)
    why_it_matters_zh: str = Field(
        default="", description="为什么值得关注，不超过 120 字", max_length=200
    )
    discipline: Discipline = "综合"
    importance: int = Field(default=5, description="1-10，10 最重要", ge=1, le=10)
    tags: list[str] = Field(default_factory=list, description="0-3 个标签")

    @field_validator("discipline", mode="before")
    @classmethod
    def _normalize_discipline(cls, v):
        if not isinstance(v, str):
            return "综合"
        return _DISCIPLINE_ALIASES.get(v.strip().lower(), _DISCIPLINE_ALIASES.get(v.strip(), "综合"))

    @field_validator("headline_zh", "summary_zh")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("不能为空")
        return v.strip()

    @field_validator("title_en", mode="before")
    @classmethod
    def _title_default(cls, v):
        return (v or "").strip()


class Digest(BaseModel):
    overview_zh: str = Field(description="全局综述，3-5 句，点出今天最值得注意的动向", max_length=800)
    items: list[DigestItem] = Field(
        default_factory=list, description="所有条目，混在一起即可；分节由程序按来源通道完成"
    )
    watchlist: list[str] = Field(default_factory=list, description="值得继续关注的线索，0-4 条")

    @field_validator("overview_zh")
    @classmethod
    def _overview_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("综述不能为空")
        return v.strip()


_SYSTEM = """你是一位科研情报编辑，为一线的科研人员写一份每日科学前沿简报。

你的读者是各领域的研究者。他们关心的是：这项工作的**核心贡献是什么**、
**方法上的新意在哪**、**对这个领域意味着什么**。

严格的写作纪律：

1. **只依据给定材料。** 候选列表是你唯一的信息来源。不要补充任何列表中
   没有的背景、数字、机构名或结论。不知道就写不知道。
2. **英文标题逐字照抄。** title_en 必须和候选行里的原标题完全一致。
   你的任务是提供中文的解读，不是翻译标题。读者要拿原标题去检索原文。
3. **预印本要标注。** 候选行里标了「预印本」的，摘要中必须体现它尚未
   经过同行评审。
4. **不要复述标题当摘要。** 摘要要给出标题里没有的信息：
   做了什么、怎么做、结果如何。
5. **不要为了凑数而写。** 材料单薄就让它简短，不要用「该方法具有重要
   意义」这类空话填充。
6. **为什么重要要具体。** 不要说「推动了领域发展」，要说清楚
   它改变了什么、让什么变得可能。

refs 必须指向真实存在的候选编号。引用不存在的编号会让整条被丢弃。"""


def build_client(provider: str | None = None) -> LLMClient:
    return LLMClient(get_config(provider))


def _mock_digest_script():
    """给 mock 后端的脚本：从 prompt 里读出候选，回一个合法的 Digest。

    ★ 为什么值得写这个：
      默认的 mock 只会回一句「脚本已用完」，于是 `--smoke` 每次都走到
      「结构化输出失败 -> 降级为规则模式」那条路。结果是冒烟测试
      **只测了兜底路径，没测真正要走的那条路**——
      prompt 构造、JSON 抽取、pydantic 校验、refs 回填，全都没被执行。

      这个脚本让 --smoke 变成一次真正的端到端：不联网、不花钱，
      但完整走过结构化输出的每一环。

    脚本可以是一个 callable，收到 messages 后返回内容——
    这就是 MockLLM 支持 `(messages) -> item` 的用处。
    """
    import re as _re

    def _build(messages: list[dict]):
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = m.get("content") or ""
                break

        items = []
        for line in user.splitlines():
            match = _re.match(r"^\[(\d+)\]\s+(.+?)(?:\s+\||\s*$)", line.strip())
            if not match:
                continue
            ref = int(match.group(1))
            title = match.group(2).strip()
            items.append(
                {
                    "refs": [ref],
                    "title_en": title[:300],
                    "headline_zh": _clip(title, 40),
                    "summary_zh": _clip(f"（离线冒烟）{title}", 200),
                    "why_it_matters_zh": "离线冒烟模式，未调用真实模型。",
                    "discipline": "综合",
                    "importance": 5,
                    "tags": [],
                }
            )
            if len(items) >= 12:
                break

        import json as _json

        return {
            "text": _json.dumps(
                {
                    "overview_zh": "（离线冒烟）本次运行未联网、未调用真实模型，"
                    f"由 mock 后端回放 {len(items)} 条候选。",
                    "items": items,
                    "watchlist": [],
                },
                ensure_ascii=False,
            )
        }

    return [_build]


def build_client_chain(primary: str | None = None) -> list[tuple[str, LLMClient]]:
    """主后端 + 可选兜底后端。构建失败的跳过并告警，不中断。"""
    chain: list[tuple[str, LLMClient]] = []
    names = [primary or os.getenv("LLM_PROVIDER", "mock")]
    fallback = os.getenv("LLM_FALLBACK_PROVIDER", "").strip()
    if fallback and fallback not in names:
        names.append(fallback)

    for name in names:
        try:
            if name == "github":
                chain.append((name, LLMClient(_github_models_config())))
            elif name == "mock":
                chain.append((name, LLMClient(get_config("mock"), mock_script=_mock_digest_script())))
            else:
                chain.append((name, build_client(name)))
        except (ConfigError, ValueError) as e:
            warn(f"后端 {name} 构建失败，跳过：{e}")
    return chain


def _github_models_config() -> Config:
    """GitHub Models 走 OpenAI 兼容协议，但 key/base/model 是自己的变量名。

    复用 deepseek 的协议分支，不改 common/ —— 原项目的同一处做法。
    """
    token = os.getenv("GITHUB_MODELS_TOKEN", "").strip()
    if not token:
        raise ConfigError("GITHUB_MODELS_TOKEN 为空。请检查它是否建成了 Variable 而不是 Secret。")
    return Config(
        provider="deepseek",
        model=os.getenv("GITHUB_MODELS_MODEL", "openai/gpt-4o-mini"),
        base_url=os.getenv("GITHUB_MODELS_URL", "https://models.github.ai/inference"),
        api_key=token,
        max_steps=int(os.getenv("MAX_STEPS", "8")),
        temperature=float(os.getenv("TEMPERATURE", "0.7")),
        anthropic_max_tokens=2048,
    )


def analyze(
    candidates: list[Candidate],
    settings: Settings,
    *,
    chain: list[tuple[str, LLMClient]] | None = None,
) -> tuple[Digest, str]:
    """候选集 -> 结构化 Digest。返回 (digest, 后端名)。"""
    if not candidates:
        return rule_based_digest([], settings), "empty"

    chain = chain if chain is not None else build_client_chain()
    if not chain:
        raise RuntimeError("没有任何可用的 LLM 后端")

    messages = _build_messages(candidates, settings)
    last_error: Exception | None = None

    for name, client in chain:
        try:
            result = call_structured(
                client,
                messages,
                Digest,
                attempts=3,
                temperature=0.2,
            )
            return result.value, name
        except StructuredError as e:
            last_error = e
            warn(f"后端 {name} 结构化输出失败（{e.attempts} 次）：{e.last_error[:160]}")
        except Exception as e:  # noqa: BLE001 - 换下一个后端再试
            last_error = e
            warn(f"后端 {name} 调用失败（{classify_error(e)}）：{str(e)[:160]}")

    raise RuntimeError(f"所有 LLM 后端都失败：{last_error}")


def _build_messages(candidates: list[Candidate], settings: Settings) -> list[dict]:
    lines = [
        f"今天是 {settings.timezone} 时区的报告日。以下是今天抓到的候选条目，"
        "按通道分组。请为其中值得报告的条目写中文简报。",
        "",
    ]

    by_channel: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_channel.setdefault(c.channel, []).append(c)

    labels = {
        "news": "【科学要闻】来自科学媒体的报道",
        "paper": "【顶刊论文】刚在目标期刊上线",
        "hot": "【热文】发表不超过两年、近期被密集引用",
        "preprint": "【预印本】arXiv，未经同行评审",
        "upgrade": "【版本升级】此前以预印本出现过，现已正式发表",
    }
    for channel in ("upgrade", "hot", "paper", "preprint", "news"):
        group = by_channel.get(channel)
        if not group:
            continue
        lines.append(f"### {labels.get(channel, channel)}")
        for c in group:
            lines.append(c.to_prompt_line())
        lines.append("")

    lines.append(
        f"请输出一份简报：overview_zh（3-5 句综述）、items（每条给出 refs / title_en / "
        f"headline_zh / summary_zh / why_it_matters_zh / discipline / importance / tags）、"
        f"watchlist（0-4 条）。"
    )
    lines.append(
        f"总量控制在 {sum(settings.channel_quota(c) for c in by_channel)} 条以内，"
        "优先选重要的，宁少勿滥。"
    )
    lines.append("再次强调：title_en 必须逐字照抄候选行里的英文原标题。")

    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]


def rule_based_digest(candidates: list[Candidate], settings: Settings) -> Digest:
    """不调模型的兜底路径。

    产出一定比模型差，但**一定存在**。原项目的取舍是对的：
    一个粗糙的报告比没有报告好，而且它明确标着 backend=rule，
    读者知道该降低期待。
    """
    items: list[DigestItem] = []
    for c in candidates:
        rec = c.record
        title = rec.title
        body = getattr(rec, "abstract", "") or getattr(rec, "summary", "") or NO_ABSTRACT
        discipline = getattr(rec, "discipline", "综合")
        if discipline not in _DISCIPLINES:
            discipline = "综合"
        # ★ 取「配置上限」与「字段上限」的较小值。中文字段就按中文字段的上限来，
        #   不能拿英文标题的配置上限去截中文字段。
        headline_limit = min(
            int(settings.report.get("max_title_chars", _LIMIT_HEADLINE_ZH)),
            _LIMIT_HEADLINE_ZH,
        )
        summary_limit = min(
            int(settings.report.get("max_summary_chars", _LIMIT_SUMMARY_ZH)),
            _LIMIT_SUMMARY_ZH,
        )
        items.append(
            DigestItem(
                refs=[c.ref],
                title_en=_clip(title, _LIMIT_TITLE_EN),
                headline_zh=_clip(title, headline_limit),
                summary_zh=_clip(body, summary_limit) or NO_ABSTRACT,
                why_it_matters_zh="",
                discipline=discipline,
                importance=5,
                tags=[],
            )
        )
    return Digest(
        overview_zh=f"本次以规则模式生成，共 {len(items)} 条。未调用大模型，摘要为原文截取。",
        items=items,
        watchlist=[],
    )


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0:
        return text
    if len(text) <= limit:
        return text
    # ★ 给省略号留一位。原项目的真实事故：_clip(text, 60) 没留位，
    #   产出 61 字符撞上 max_length=60 的 pydantic 字段，直接崩。
    return text[: limit - 1].rstrip() + "…"
