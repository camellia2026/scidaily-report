"""引用速度：把「热文」和「名篇」分开（增强点 4）。

**问题的形状。** 原项目只按新鲜度排序，对科学文献不够：一篇半年前发表、
最近被密集引用的论文，比昨天刚上线的一篇 TPAMI 更值得看。

但「按引用数排序」是错的，而且错得很典型。实测热文候选榜首：

    JAMA《World Medical Association Declaration of Helsinki》
    总引用 1523，篇均年引用 780

它是 1964 年的文件在今天被重新发表。按总量排，它永远是第一名——
报告会天天出现同一批老文献。**那是「著名」，不是「热」。**

**解法分两层，第二层才是关键。**

第一层：**近年引用占比。**
    老名篇： 总 1523，近年约  60  → 占比 0.04
    真热文： 总   80，近年约  80  → 占比 1.00

    所以指标定义为：

        hotness = 近年引用数 × 近年引用占比

    两个因子缺一不可：
      - 只有「近年引用数」→ 老名篇（每年被引 50 次）仍然很高
      - 只有「占比」     → 一篇刚发表被引 2 次的新论文占比 1.0，会压过一切

    实测：59 个候选里，它筛掉了 4 个引用主要发生在历史上的条目。

第二层：**热文必须和新闻一样走跨天去重。**

    ★ 这一层是实测逼出来的，不是设计时想到的。
      第一层并没有把《赫尔辛基宣言》拉下来——实测它的 counts_by_year 是
      近年 1493 / 总 1523，占比 98%。原因是 OpenAlex 把 2024 年 JAMA 的
      重新发表当成一个**独立的新 work**，所以它在 OpenAlex 眼里确实是
      「新近且高引」的，占比法对它无效。

      真正的问题是**粘性**：一篇热文会在榜单上停留几个月。
      没有去重的话，同一篇论文会天天出现在报告里——
      这正是原项目「日报和信息洪流的区别」那条教训在科学文献上的重演。

      所以热文通道和新闻通道走同一套 seen 去重：
      一篇论文第一次跨过热度阈值时报一次，之后静默。
      《赫尔辛基宣言》因此只会出现一次，这是正确的行为。

**年龄上限是第二道闸。** OpenAlex 只给年度粒度，所以「近年」只能是自然年；
年龄上限 24 个月保证候选池里不会有纯粹的史料。

**成本（直说）。** counts_by_year 随主请求一起返回，Tier A 零额外成本。
Tier B 才需要每篇一个请求去精查「近 90 天被引」，只对短名单做。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .settings import Settings
from .sources import openalex
from .sources.base import Paper


@dataclass
class HotScore:
    recent: int           # 近年引用数
    ratio: float          # 近年引用 / 总引用
    hotness: float        # = recent * ratio
    age_months: float

    def explain(self) -> str:
        return f"近年 {self.recent} 次 · 占比 {self.ratio:.0%} · 热度 {self.hotness:.1f}"


def compute_hotness(paper: Paper, now: datetime) -> HotScore | None:
    """算一篇论文的热度。没有 counts_by_year 或年龄超限就返回 None。"""
    age = _age_months(paper.published, now)
    if age is None:
        return None
    total = max(paper.citation_count, 0)
    if total <= 0:
        return None

    recent = _recent_citations(paper.counts_by_year, now)
    if recent <= 0:
        return None

    ratio = recent / total
    return HotScore(
        recent=recent,
        ratio=ratio,
        hotness=recent * ratio,
        age_months=age,
    )


def _recent_citations(counts_by_year: dict[int, int], now: datetime) -> int:
    """「近年」= 当前自然年 + 上一个自然年。

    ★ 为什么是两年而不是一年：counts_by_year 只有年度粒度，
      年初跑的时候当前年只有几周数据，单独用它会把所有论文都算成 0。
      取两年窗口在「够新」和「有信号」之间取得平衡。
    """
    year = now.year
    return int(counts_by_year.get(year, 0)) + int(counts_by_year.get(year - 1, 0))


def _age_months(published: datetime | None, now: datetime) -> float | None:
    if published is None:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=now.tzinfo)
    delta = now - published
    return delta.total_seconds() / (86400.0 * 30.44)


def rank_hot(
    papers: list[Paper], settings: Settings, *, now: datetime
) -> list[tuple[Paper, HotScore]]:
    """按热度排序，并应用年龄上限。"""
    max_age = float(settings.velocity.get("max_age_months", 24))
    scored: list[tuple[Paper, HotScore]] = []
    for p in papers:
        score = compute_hotness(p, now)
        if score is None:
            continue
        if score.age_months > max_age:
            continue
        scored.append((p, score))
    scored.sort(key=lambda pair: pair[1].hotness, reverse=True)
    return scored


def refine_with_recent_window(
    ranked: list[tuple[Paper, HotScore]],
    settings: Settings,
    *,
    now: datetime,
    offline: bool = False,
) -> list[tuple[Paper, HotScore]]:
    """Tier B：对短名单做「近 N 天被引」的精确统计。

    Tier A（counts_by_year）只有年度粒度。Tier B 用 OpenAlex 的 cites: 过滤器
    真去数一遍——每篇一个请求，所以只做短名单。

    这是**增强**而不是**必需**：Tier B 失败（限流、网络）时保留 Tier A 的排序，
    不抛异常。一个用来锦上添花的精查，不该有能力把整条流水线搞挂。
    """
    shortlist = int(settings.velocity.get("shortlist", 10))
    window_days = int(settings.velocity.get("recent_window_days", 90))
    since = now - timedelta(days=window_days)

    refined: list[tuple[Paper, HotScore]] = []
    for i, (paper, score) in enumerate(ranked):
        if i < shortlist and paper.work_id:
            count = openalex.count_recent_citations(
                paper.work_id, since, settings, offline=offline
            )
            if count is not None:
                paper.recent_citations = count
                # 精确值若明显低于年度估算，说明年度窗口把老论文的引用算多了，
                # 用精确值重算，避免虚高的老文献挤进榜单。
                if count < score.recent:
                    score = HotScore(
                        recent=count,
                        ratio=count / max(1, paper.citation_count),
                        hotness=count * (count / max(1, paper.citation_count)),
                        age_months=score.age_months,
                    )
        refined.append((paper, score))

    refined.sort(key=lambda pair: pair[1].hotness, reverse=True)
    return refined
