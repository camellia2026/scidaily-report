"""渲染 + 反幻觉校验。

★ 这是整条流水线里最重要的一个模块。

它拦的是**编造引用**，不是编造内容。具体能保证：
  - 模型引用的编号真实存在（refs 越界 -> 丢弃该条）
  - 报告里出现的 URL 都在本次抓取集里（URL 泄漏检查）
  - 摘要不是雷同的填充文字

它**不能**保证模型把某篇论文总结对了。真正的防线是：
短抽取式摘要 + 每条带原始链接 + 你自己看一眼。
报告末尾那句免责声明是认真的。

分节规则：条目的节由它来自哪个**通道**决定，不由模型决定
（见 ai.py 的说明）。这里用 ref -> channel 的映射补上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ai import NO_ABSTRACT, Digest, DigestItem
from .filter import Candidate, FilterStats
from .settings import Settings
from .sources.base import SourceReport

DROP_RATIO_THRESHOLD = 0.30
SHORT_SUMMARY_CHARS = 20

# ★ 不要排除圆括号。实测踩到：Cell 的链接形如
#     https://www.cell.com/cell/fulltext/S0092-8674(26)00845-1
#   把 `)` 排除在字符类外，正则会在第一个 `)` 处截断，
#   得到一个**真实存在但不完整**的 URL——
#   于是「URL 泄漏检查」把一条合法链接报成伪造链接，
#   而渲染出的锚点也指向 404。这类假阳性比漏报更费时间，
#   因为它看起来像模型在编造链接。
#   改为：宽匹配，再在收尾时剥掉尾部的标点与不成对的右括号。
_URL_RE = re.compile(r"https?://[^\s<>「」【】]+")
_TRAILING_JUNK = re.compile(r"[.,;:!?，。；：！？]+$")


def _clean_url(url: str) -> str:
    """剥掉尾部标点，以及**不成对**的右括号。

    成对的括号要保留（Cell / Wiley 的 DOI 式链接里很常见）；
    只有多出来的那些才是句末标点。
    """
    url = _TRAILING_JUNK.sub("", url)
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url

# 节的渲染顺序与标题
SECTIONS: list[tuple[str, str]] = [
    ("upgrade", "版本升级（预印本 → 正式发表）"),
    ("hot", "热度上升"),
    ("paper", "顶刊论文"),
    ("preprint", "预印本前沿（未经同行评审）"),
    ("news", "科学要闻"),
]


@dataclass
class GroundingReport:
    ok: bool = True
    out_of_range: list[int] = field(default_factory=list)
    url_leaks: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    kept: int = 0
    dropped: int = 0
    drop_ratio: float = 0.0

    def describe(self) -> str:
        if self.ok and not self.problems:
            return f"校验通过（保留 {self.kept} 条）"
        bits = [f"保留 {self.kept} / 丢弃 {self.dropped}（{self.drop_ratio:.0%}）"]
        if self.out_of_range:
            bits.append(f"越界引用 {self.out_of_range}")
        if self.url_leaks:
            bits.append(f"未验证链接 {len(self.url_leaks)} 条")
        bits.extend(self.problems)
        return "；".join(bits)


def sanitize(
    digest: Digest, candidates: list[Candidate], settings: Settings
) -> tuple[Digest, GroundingReport]:
    """丢掉引用越界的条目，并评估整体质量。

    ★ 单条越界 -> 丢弃该条，不是全量否决。
      原项目的取舍：全量一票否决会因为一条坏引用让整个早上没有报告。
      但丢弃率过高（>30%）说明模型整体没跟上，那就该判定失败、不推送。
    """
    report = GroundingReport()
    valid_refs = {c.ref for c in candidates}

    kept: list[DigestItem] = []
    for item in digest.items:
        bad = [r for r in item.refs if r not in valid_refs]
        if bad:
            report.out_of_range.extend(bad)
            report.dropped += 1
            continue
        kept.append(item)

    report.kept = len(kept)
    total = report.kept + report.dropped
    report.drop_ratio = (report.dropped / total) if total else 0.0

    if report.drop_ratio > DROP_RATIO_THRESHOLD:
        report.problems.append(f"引用越界率 {report.drop_ratio:.0%} 超过阈值")

    _check_summaries(kept, report)

    report.ok = not report.problems
    return Digest(overview_zh=digest.overview_zh, items=kept, watchlist=digest.watchlist), report


def _check_summaries(items: list[DigestItem], report: GroundingReport) -> None:
    """检查摘要质量。

    ★ 这里有一个原项目踩过的坑，也是本项目的测试要钉住的：
      当某天只有 1 条候选、且它的摘要恰好是源的占位文字
      「(该源未提供摘要)」（8 字符）时，
      「1 条里 1 条过短 = 100%」会把整份报告判成不合格。
      修法是两条：占位符排除在检查外；样本 < 3 时不做比例判断。
    """
    real = [i for i in items if i.summary_zh.strip() and i.summary_zh.strip() != NO_ABSTRACT]
    if len(real) < 3:
        return

    short = [i for i in real if len(i.summary_zh.strip()) < SHORT_SUMMARY_CHARS]
    if len(short) / len(real) > 0.5:
        report.problems.append(f"{len(short)}/{len(real)} 条摘要过短")

    seen: dict[str, int] = {}
    for i in real:
        key = i.summary_zh.strip()
        seen[key] = seen.get(key, 0) + 1
    dup = sum(n - 1 for n in seen.values() if n > 1)
    if dup and dup / len(real) > 0.3:
        report.problems.append(f"{dup} 条摘要雷同")


def check_urls(markdown: str, known_urls: set[str]) -> list[str]:
    """找出报告里出现、但本次并没有抓到的 URL。

    模型编造链接是很常见的失败模式，而且比编造内容更危险——
    读者会点进去。所以这一条是硬检查。
    """
    found = {_clean_url(u) for u in _URL_RE.findall(markdown)}
    return sorted(u for u in found if u not in known_urls)


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def render_markdown(
    date: str,
    digest: Digest,
    candidates: list[Candidate],
    settings: Settings,
    source_reports: list[SourceReport],
    stats_by_channel: dict[str, FilterStats],
    *,
    grounding: GroundingReport | None = None,
    backend: str = "rule",
    staleness_hours: float | None = None,
    is_empty_day: bool = False,
) -> str:
    by_ref = {c.ref: c for c in candidates}
    grouped = _group_by_section(digest, by_ref)

    lines: list[str] = [f"# 每日科学前沿 · {date}", ""]
    lines.append(f"> 生成时间：{_now_str(settings)} | 后端：{backend} | 时区：{settings.timezone}")
    parts = [
        f"{len(grouped[ch])} 条{label}"
        for ch, label in SECTIONS
        if grouped.get(ch)
    ]
    lines.append(f"> {' · '.join(parts) if parts else '今日无条目'}")
    lines.append("")

    if is_empty_day:
        lines.append("> **今日无新条目。** 这通常意味着所有源都没抓到窗口内的内容，")
        lines.append("> 或者源全部失败——请先看下面的「数据源状态」表再下结论。")
        lines.append("")
    if grounding is not None and not grounding.ok:
        lines.append(f"> ⚠️ **校验未通过：** {'；'.join(grounding.problems)}。本条报告仅供参考。")
        lines.append("")
    if staleness_hours is not None and staleness_hours > 36:
        lines.append(f"> ⚠️ 距上次成功运行已 {staleness_hours:.0f} 小时，中间可能有漏报。")
        lines.append("")

    lines.append("## 今日综述")
    lines.append("")
    lines.append(digest.overview_zh)
    lines.append("")

    for channel, label in SECTIONS:
        section_items = grouped.get(channel) or []
        if not section_items:
            continue
        lines.append(f"## {label}")
        lines.append("")
        for idx, item in enumerate(section_items, start=1):
            _render_item(lines, idx, item)
        lines.append("")

    if digest.watchlist:
        lines.append("## 值得继续关注")
        lines.append("")
        for entry in digest.watchlist:
            lines.append(f"- {entry}")
        lines.append("")

    _render_sources(lines, source_reports, stats_by_channel, settings)
    _render_links(lines, candidates)

    lines.append("---")
    lines.append("")
    lines.append(
        "*本报告由 GitHub Actions 自动生成。中文摘要由大模型依据上述来源的标题与"
        "摘要改写，可能存在理解偏差；**英文标题与原文链接为准**。"
        "预印本未经同行评审，引用前请自行核实。*"
    )
    return "\n".join(lines)


def _group_by_section(
    digest: Digest, by_ref: dict[int, Candidate]
) -> dict[str, list[DigestItem]]:
    """按条目引用的候选所属通道分节。

    ★ 这里体现了「不让模型决定分节」的价值：即使模型把一条预印本写进了
      值得注意的论文里，它也会被放回「预印本前沿」那一节。
    """
    grouped: dict[str, list[DigestItem]] = {ch: [] for ch, _ in SECTIONS}
    for item in digest.items:
        channel = "news"
        for ref in item.refs:
            cand = by_ref.get(ref)
            if cand is not None:
                channel = cand.channel
                break
        grouped.setdefault(channel, []).append(item)

    order = {"upgrade": 0, "hot": 1, "paper": 2, "preprint": 3, "news": 4}
    for items in grouped.values():
        items.sort(key=lambda i: (-i.importance, order.get("", 0)))
    return grouped


def _render_item(lines: list[str], idx: int, item: DigestItem) -> None:
    lines.append(f"### {idx}. {item.headline_zh}  `{item.discipline}` `重要度 {item.importance}/10`")
    lines.append("")
    if item.title_en and item.title_en != item.headline_zh:
        lines.append(f"**{item.title_en}**")
        lines.append("")
    lines.append(item.summary_zh)
    lines.append("")
    if item.why_it_matters_zh:
        lines.append(f"**为什么重要：** {item.why_it_matters_zh}")
        lines.append("")
    tags = " ".join(f"`{t}`" for t in item.tags if t)
    refs = " / ".join(f"[{r}]" for r in item.refs)
    lines.append(f"<sub>来源：{refs}{('  ' + tags) if tags else ''}</sub>")
    lines.append("")


def _render_sources(
    lines: list[str],
    reports: list[SourceReport],
    stats_by_channel: dict[str, FilterStats],
    settings: Settings,
) -> None:
    if not reports:
        return
    lines.append("## 数据源状态")
    lines.append("")
    lines.append("| 源 | 状态 | 抓到 | 留存 | 耗时 |")
    lines.append("|---|---|---:|---:|---:|")
    for r in sorted(reports, key=lambda x: (x.ok, x.id)):
        mark = "OK" if r.ok else "**失败**"
        note = f" {r.error[:40]}" if r.error else ""
        lines.append(
            f"| {r.name} | {mark} {r.status}{note} | {r.fetched} | {r.kept} | {r.elapsed_s:.1f}s |"
        )
    lines.append("")
    for channel, _ in SECTIONS:
        stats = stats_by_channel.get(channel)
        if stats is not None and stats.fetched:
            lines.append(f"- `{channel}` {stats.summary_line()}")
    lines.append("")


def _render_links(lines: list[str], candidates: list[Candidate]) -> None:
    if not candidates:
        return
    lines.append("## 原始链接")
    lines.append("")
    seen: set[str] = set()
    for c in sorted(candidates, key=lambda x: x.ref):
        rec = c.record
        url = getattr(rec, "url", "") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        lines.append(f"{c.ref}. [{rec.title}]({url})")
    lines.append("")


def _now_str(settings: Settings) -> str:
    from .settings import now_local

    return now_local(settings).strftime("%Y-%m-%d %H:%M %Z")


# --------------------------------------------------------------------------
# JSON 孪生 / HTML
# --------------------------------------------------------------------------


def build_report_json(
    date: str,
    digest: Digest,
    candidates: list[Candidate],
    source_reports: list[SourceReport],
    stats_by_channel: dict[str, FilterStats],
    settings: Settings,
    *,
    grounding: GroundingReport | None = None,
    backend: str = "rule",
    sent: bool = False,
) -> dict:
    """给程序看的孪生。字段与原项目保持一致，便于跨项目比对。"""
    by_ref = {c.ref: c for c in candidates}
    items = []
    for item in digest.items:
        records = []
        for ref in item.refs:
            cand = by_ref.get(ref)
            if cand is None:
                continue
            rec = cand.record
            records.append(
                {
                    "ref": ref,
                    "channel": cand.channel,
                    "title": rec.title,
                    "url": getattr(rec, "url", ""),
                    "journal": getattr(rec, "journal", "") or getattr(rec, "source_name", ""),
                    "tier": getattr(rec, "tier", ""),
                    "doi": getattr(rec, "doi", ""),
                    "published": rec.published.isoformat() if rec.published else None,
                }
            )
        items.append(
            {
                **item.model_dump(),
                "records": records,
            }
        )

    return {
        "date": date,
        "generated_at": _now_str(settings),
        "backend": backend,
        "sent": sent,
        "window_hours": {
            "news": settings.news_window_hours,
            "paper": settings.paper_window_hours,
        },
        "overview_zh": digest.overview_zh,
        "items": items,
        "watchlist": digest.watchlist,
        "grounding": {
            "ok": grounding.ok if grounding else True,
            "problems": grounding.problems if grounding else [],
            "out_of_range": grounding.out_of_range if grounding else [],
            "url_leaks": grounding.url_leaks if grounding else [],
        },
        "stats": {ch: vars(stats) for ch, stats in stats_by_channel.items()},
        "sources": [r.to_dict() for r in source_reports],
    }


def render_html(markdown: str, date: str) -> str:
    """邮件用 HTML。

    ★ 不依赖外部 CSS —— 邮件客户端会剥离 <style> 和外部样式表。
      所以样式全部内联，手写转换器，不引入 markdown 库
      （原项目同一取舍：那个带侧边栏的整站生成器在这里用不上）。
    """
    body = _md_to_html(markdown)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>每日科学前沿 {date}</title></head>"
        "<body style=\"margin:0;padding:16px;background:#fff;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',"
        "'Microsoft YaHei',sans-serif;font-size:15px;line-height:1.7;color:#1a1a1a\">"
        f"<div style='max-width:760px;margin:0 auto'>{body}</div>"
        "</body></html>"
    )


def _md_to_html(md: str) -> str:
    out: list[str] = []
    in_table = False
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def close_table() -> None:
        nonlocal in_table
        if in_table:
            out.append("</table>")
            in_table = False

    for raw in md.split("\n"):
        line = raw.rstrip()

        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if not in_table:
                close_list()
                out.append(
                    "<table style='border-collapse:collapse;width:100%;font-size:13px;"
                    "margin:8px 0'>"
                )
                in_table = True
                tag = "th"
            else:
                tag = "td"
            tds = "".join(
                f"<{tag} style='border:1px solid #ddd;padding:6px 8px;text-align:left'>{_inline(c)}</{tag}>"
                for c in cells
            )
            out.append(f"<tr>{tds}</tr>")
            continue
        close_table()

        if line.startswith("- "):
            if not in_list:
                out.append("<ul style='padding-left:20px;margin:8px 0'>")
                in_list = True
            out.append(f"<li>{_inline(line[2:])}</li>")
            continue
        close_list()

        if line.startswith("# "):
            out.append(
                f"<h1 style='font-size:22px;margin:0 0 12px;border-bottom:2px solid #222;"
                f"padding-bottom:8px'>{_inline(line[2:])}</h1>"
            )
        elif line.startswith("## "):
            out.append(
                f"<h2 style='font-size:18px;margin:24px 0 10px;padding-left:8px;"
                f"border-left:4px solid #c2571a'>{_inline(line[3:])}</h2>"
            )
        elif line.startswith("### "):
            out.append(f"<h3 style='font-size:16px;margin:18px 0 6px'>{_inline(line[4:])}</h3>")
        elif line.startswith("> "):
            out.append(
                "<blockquote style='margin:8px 0;padding:8px 12px;background:#f6f6f4;"
                f"border-left:3px solid #bbb;color:#444;font-size:13px'>{_inline(line[2:])}</blockquote>"
            )
        elif line.strip() == "---":
            out.append("<hr style='border:none;border-top:1px solid #ddd;margin:20px 0'>")
        elif line.strip():
            out.append(f"<p style='margin:8px 0'>{_inline(line)}</p>")

    close_list()
    close_table()
    return "".join(out)


_INLINE_RULES = [
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"`([^`]+)`"), r"<code style='background:#f0f0ee;padding:1px 5px;border-radius:3px;font-size:13px'>\1</code>"),
    (re.compile(r"<sub>(.+?)</sub>"), r"<small style='color:#777'>\1</small>"),
    (re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)"), r"<a href='\2' style='color:#c2571a'>\1</a>"),
    (re.compile(r"(?<![=\"'])(https?://[^\s<]+)"), r"<a href='\1' style='color:#c2571a'>\1</a>"),
]


def _inline(text: str) -> str:
    for pattern, repl in _INLINE_RULES:
        text = pattern.sub(repl, text)
    return text
