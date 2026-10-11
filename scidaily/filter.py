"""去重与规则预打分。

送进 LLM 之前的漏斗。这一步同时是三个杠杆：
    成本杠杆 —— 上下文里的原始素材越少，token 越少
    延迟杠杆 —— 候选越少，模型越快
    反幻觉杠杆 —— 原始素材越少，可编造的空间越小
（这一条是从原项目直接继承的，它成立的原因不随知识域改变。）

★ 候选编号 ref 在**截断之后**才分配。这不是细节：
  先编号再截断的话，prompt 里会出现 ref 跳号（1,2,5,7...），
  模型引用时容易错位，而错位在渲染阶段会被当成「越界引用」整条丢掉。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .settings import Settings
from .sources.base import NewsItem, Paper
from .state import SeenState, normalize_title

Record = Paper | NewsItem


@dataclass
class Candidate:
    """一个候选条目。ref 是它与 LLM 之间的唯一契约。"""

    ref: int
    channel: str
    record: Record
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.record.title

    @property
    def age_hours(self) -> float | None:
        return self.record.age_hours

    def to_prompt_line(self) -> str:
        """喂给模型的一行。这是模型能看到的全部现实——它编不出别的。"""
        rec = self.record
        bits = [f"[{self.ref}]", rec.title]
        if isinstance(rec, Paper):
            meta = [rec.journal, rec.tier, rec.discipline]
            if rec.venue_type == "preprint":
                meta.append("预印本")
            if rec.citation_count:
                meta.append(f"被引 {rec.citation_count}")
            bits.append("| " + " · ".join(m for m in meta if m))
            if rec.abstract:
                bits.append(f"| 摘要：{rec.abstract}")
        else:
            bits.append(f"| {rec.source_name}")
            if rec.summary:
                bits.append(f"| 摘要：{rec.summary}")
        return " ".join(bits)


@dataclass
class FilterStats:
    fetched: int = 0
    dup_in_run: int = 0
    already_seen: int = 0
    dropped_by_penalty: int = 0
    dropped_by_window: int = 0
    unique: int = 0
    candidates: int = 0

    def summary_line(self) -> str:
        return (
            f"抓取 {self.fetched} → 运行内重复 {self.dup_in_run} / 历史已见 "
            f"{self.already_seen} / 窗口外 {self.dropped_by_window} "
            f"/ 惩罚词 {self.dropped_by_penalty} → 去重后 {self.unique} "
            f"→ 送模型 {self.candidates}"
        )


# --------------------------------------------------------------------------
# 预打分
# --------------------------------------------------------------------------


def pre_score(record: Record, settings: Settings, now: datetime) -> tuple[float, list[str]]:
    """规则预打分。返回 (分数, 理由列表)。

        score = 权重 × 新鲜度 × 速度加成 × 关键词因子

    权重来自期刊分级（T0=3.0/T1=2.0/T2=1.4）或新闻源权重。
    """
    reasons: list[str] = []
    score = float(getattr(record, "weight", 1.0) or 1.0)
    if score != 1.0:
        reasons.append(f"权重{score:.1f}")

    fresh = _freshness(record, settings, now)
    score *= fresh
    reasons.append(f"新鲜{fresh:.2f}")

    if isinstance(record, Paper):
        boost = _velocity_factor(record, settings)
        if boost != 1.0:
            score *= boost
            reasons.append(f"速度x{boost:.2f}")

    factor = _keyword_factor(record, settings)
    if factor != 1.0:
        score *= factor
        reasons.append(f"关键词x{factor:.2f}")

    return score, reasons


def _freshness(record: Record, settings: Settings, now: datetime) -> float:
    """指数衰减：0.5^(age/half_life)。

    ★ 论文与新闻必须用不同的半衰期，这是有意的：
      一篇三天前的 Nature 论文仍然值得看，三天前的科技新闻就不一定了。
      同一个半衰期会让两者要么一起太快变旧（新闻合适、论文被埋），
      要么一起太慢（论文合适、陈年新闻混进来）。
    """
    if isinstance(record, Paper):
        half_life = float(
            settings.prefilter.get(
                "freshness_half_life_h", settings.prefilter.get("freshness_half_life_h_paper", 48)
            )
        )
    else:
        half_life = float(settings.prefilter.get("freshness_half_life_h_news", 12))

    if half_life <= 0:
        return 1.0
    age = record.age_hours
    if age is None:
        # 没有日期：不奖不罚，取中间值。
        # 直接给 1.0 会让无日期条目永远排在最前，给 0 又等于丢弃。
        return 0.5
    if age < 0:  # 未来时间，多半是源写错了，按最新处理
        age = 0.0
    return 0.5 ** (age / half_life)


def _velocity_factor(paper: Paper, settings: Settings) -> float:
    """引用速度加成，对数压缩。

    用 log1p 是因为引用数是重尾分布：不压缩的话一篇万引论文会把
    排序整个压平，其他论文之间的差别全部消失。

    基准取 10 次/年（一个「有人在用」的论文的常见量级），
    所以 10 次/年是 1.0，100 次/年约 1.6，1000 次/年约 2.2。
    """
    cpy = max(0.0, paper.cites_per_year)
    if cpy <= 0:
        return 1.0
    baseline = 10.0
    factor = 1.0 + 0.6 * math.log1p(cpy) / math.log1p(baseline)
    return min(factor, 2.5)


def _keyword_factor(record: Record, settings: Settings) -> float:
    text = (record.title or "") + " " + _body_text(record)
    lowered = text.lower()

    for word in settings.prefilter.get("penalty_keywords") or []:
        if str(word).lower() in lowered:
            return float(settings.prefilter.get("keyword_penalty", 0.4))

    for word in settings.prefilter.get("keywords") or []:
        if str(word).lower() in lowered:
            return float(settings.prefilter.get("keyword_boost", 2.0))
    return 1.0


def _body_text(record: Record) -> str:
    if isinstance(record, Paper):
        return record.abstract or ""
    return record.summary or ""


def _is_penalised(record: Record, settings: Settings) -> bool:
    """只命中惩罚词、且没命中任何正向词 —— 直接丢。

    命中正向词时不做丢弃：一篇题为「Retraction of ...」但讨论撤稿机制的
    研究论文是存在的，交给模型判断比在规则层一刀切更稳。
    """
    text = ((record.title or "") + " " + _body_text(record)).lower()
    hit_penalty = any(
        str(w).lower() in text for w in (settings.prefilter.get("penalty_keywords") or [])
    )
    if not hit_penalty:
        return False
    hit_boost = any(
        str(w).lower() in text for w in (settings.prefilter.get("keywords") or [])
    )
    return not hit_boost


# --------------------------------------------------------------------------
# 建候选集
# --------------------------------------------------------------------------


def fingerprint(record: Record) -> str:
    if isinstance(record, Paper) and record.work_id:
        return "w:" + record.work_id
    if isinstance(record, Paper) and record.doi:
        return "d:" + record.doi.lower()
    return "t:" + normalize_title(record.title)


def build_candidates(
    records: list[Record],
    state: SeenState,
    settings: Settings,
    now: datetime,
    *,
    channel: str,
    max_age_hours: float | None = None,
    ignore_seen: bool = False,
) -> tuple[list[Candidate], FilterStats]:
    """一个通道的候选集。与来源无关，新闻和论文走同一段代码。"""
    stats = FilterStats(fetched=len(records))

    seen_fp: set[str] = set()
    unique: list[Record] = []
    for rec in records:
        fp = fingerprint(rec)
        if fp in seen_fp:
            stats.dup_in_run += 1
            continue
        seen_fp.add(fp)

        if max_age_hours is not None:
            age = rec.age_hours
            if age is not None and age > max_age_hours:
                stats.dropped_by_window += 1
                continue

        if _is_penalised(rec, settings):
            stats.dropped_by_penalty += 1
            continue

        if not ignore_seen and _is_seen(rec, state):
            stats.already_seen += 1
            continue

        unique.append(rec)

    stats.unique = len(unique)

    scored: list[Candidate] = []
    for rec in unique:
        score, reasons = pre_score(rec, settings, now)
        scored.append(Candidate(ref=0, channel=channel, record=rec, score=score, reasons=reasons))
    scored.sort(key=lambda c: c.score, reverse=True)

    cap = settings.channel_cap(channel)
    scored = scored[:cap]

    # ★ 编号必须在截断之后
    for i, cand in enumerate(scored, start=1):
        cand.ref = i

    stats.candidates = len(scored)
    return scored, stats


def _is_seen(rec: Record, state: SeenState) -> bool:
    if isinstance(rec, Paper):
        return state.is_seen(work_id=rec.work_id, doi=rec.doi, title=rec.title)
    return state.is_seen(doi=rec.doi, title=rec.title, url=rec.url)


def mark_seen(records: list[Record], state: SeenState, date: str) -> int:
    """标记为已见。**标记全部候选，不只是最终发布的那几条。**

    ★ 原项目的教训：只标记发布的条目，落选的那几十条明天会重新进候选池，
      天天占位却不产出。候选池会被自己的落选者堵死。
    """
    count = 0
    for rec in records:
        if isinstance(rec, Paper):
            ok = state.mark(
                date, work_id=rec.work_id, doi=rec.doi, title=rec.title,
                venue=rec.venue_type,
            )
        else:
            ok = state.mark(date, doi=rec.doi, title=rec.title, url=rec.url)
        if ok:
            count += 1
    return count
