"""Crossref 适配器 —— 补漏通道。

**定位要说清楚：它不是主力，也不该当主力。**
实测（2026-10-01）：
  - 摘要基本为 NONE。Nature 的文章在 Crossref 里 `abstract` 字段大量缺失，
    所以不能靠它做摘要。
  - JMLR 在 Crossref 里 total=0（该刊不在 Crossref 索引体系内）。

它的价值在别处：
  - 覆盖了 12 个目标 ISSN 的元数据，是 OpenAlex 之外的第二条独立验证线；
  - `is-referenced-by-count` 提供交叉验证引用数；
  - 当 OpenAlex 某天缺条目时，它能补上 DOI / 作者 / 出版日期。

所以它默认只在启用 crossref 通道的期刊上跑，且产出记录不带摘要。
"""

from __future__ import annotations

import time

from ..settings import Journal, Settings
from . import cache
from .base import FetchError, Paper, SourceReport, http_get_json, parse_datetime

SELECT = ",".join(
    [
        "DOI",
        "title",
        "author",
        "container-title",
        "type",
        "published",
        "issued",
        "is-referenced-by-count",
        "abstract",
        "URL",
    ]
)

MAX_ROWS = 200


def fetch_journal(
    journal: Journal, settings: Settings, *, since, offline: bool = False,
    ttl_hours: float = cache.TTL_HOURS_LOCAL,
) -> tuple[list[Paper], SourceReport]:
    report = SourceReport(id=f"crossref_{journal.id}", name=f"{journal.name} (Crossref)")
    params = {
        "filter": f"from-pub-date:{since.strftime('%Y-%m-%d')},type:journal-article",
        "rows": MAX_ROWS,
        "select": SELECT,
        "mailto": settings.api.get("mailto", ""),
        "sort": "published",
        "order": "desc",
    }
    key = cache.cache_key(f"crossref_{journal.id}", params)

    started = time.monotonic()
    try:
        payload = cache.cached_json(
            key,
            lambda: http_get_json(
                f"{settings.api['crossref_base']}/journals/{journal.issn}/works",
                params=params,
                timeout_s=float(settings.api.get("timeout_s", 45.0)),
            ),
            ttl_hours=ttl_hours,
            offline=offline,
        )
    except (FetchError, FileNotFoundError) as e:
        report.status = "cached" if isinstance(e, FileNotFoundError) else "parse_error"
        report.error = str(e)
        report.elapsed_s = time.monotonic() - started
        return [], report

    items = (((payload or {}).get("message") or {}).get("items")) or []
    papers: list[Paper] = []
    for item in items:
        paper = _to_paper(item, journal)
        if paper is not None:
            papers.append(paper)

    report.fetched = len(items)
    report.kept = len(papers)
    report.status = "ok" if papers else "empty"
    report.elapsed_s = time.monotonic() - started
    return papers, report


def _to_paper(item: dict, journal: Journal) -> Paper | None:
    titles = item.get("title") or []
    title = (titles[0] if titles else "").strip()
    if not title:
        return None

    doi = (item.get("DOI") or "").strip()
    authors = []
    for a in (item.get("author") or [])[:8]:
        name = " ".join(
            p for p in [(a.get("given") or "").strip(), (a.get("family") or "").strip()] if p
        )
        if not name:
            name = (a.get("name") or "").strip()
        if name:
            authors.append(name)

    published = _published(item)

    return Paper(
        work_id="",  # Crossref 不给 OpenAlex work id，靠 DOI 对齐
        title=title,
        url=str(item.get("URL") or (f"https://doi.org/{doi}" if doi else "")),
        journal=journal.name,
        issn=journal.issn,
        tier=journal.tier,
        discipline=journal.discipline,
        doi=doi,
        authors=authors,
        published=published,
        citation_count=int(item.get("is-referenced-by-count") or 0),
        abstract=_strip_jats(str(item.get("abstract") or ""))[:1600],
        venue_type="journal",
        source_id=f"crossref_{journal.id}",
        source_name=f"{journal.name} (Crossref)",
        weight=journal.tier_weight,
    )


def _published(item: dict):
    """Crossref 的日期字段有三个变体，按可靠性依次尝试。"""
    for field in ("published", "issued", "published-online", "published-print"):
        node = item.get(field)
        if not isinstance(node, dict):
            continue
        parts = node.get("date-parts") or []
        if parts and parts[0]:
            nums = [int(x) for x in parts[0] if str(x).isdigit()]
            nums += [1] * (3 - len(nums))
            return parse_datetime(f"{nums[0]:04d}-{nums[1]:02d}-{nums[2]:02d}")
    return None


def _strip_jats(text: str) -> str:
    """Crossref 的摘要是 JATS XML 片段，剥成纯文本。"""
    if not text:
        return ""
    import re

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower().startswith("abstract"):
        text = text[len("abstract"):].lstrip(": ").strip()
    return text
