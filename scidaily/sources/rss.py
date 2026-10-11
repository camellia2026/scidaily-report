"""期刊 TOC feed 与科学要闻的适配器。

这是唯一沿用原项目「抓 feed」思路的模块，但它在本项目里是**配角**：
顶刊里只有部分有可用 feed（JAMA 实测 403，TPAMI/IJCV/JMLR/SISC/Acta Numerica 没有），
所以 feed 只负责三件事：
  1. 补上 OpenAlex 收录滞后一天的那些条目
  2. 提供 OpenAlex 没有的编辑摘要（Nature 的 content:encoded）
  3. 科学要闻通道（期刊新闻版 + 科学媒体）

★ 实测的字段坑（2026-10-01）：
  - Nature 的 RDF 条目**没有 description 标签**，摘要在 content:encoded 里。
  - Nature 一个 feed 里混着研究论文（10.1038/s41586-）和新闻稿（10.1038/d41586-），
    靠 DOI 前缀区分——这就是 journals.yaml 里 research_doi_prefix 的用途。
  - Cell / Lancet 的 DOI 在 dc:identifier 里。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser
import httpx

from common.console import warn
from common.reliability import retry_call

from ..settings import Journal, Settings, Source
from . import cache
from .base import (
    FetchError,
    NewsItem,
    Paper,
    SourceReport,
    parse_datetime,
)

# 从链接里抠 DOI 的兜底。Nature 的 /articles/d41586-026-03124-w 就是这样还原的。
_DOI_IN_URL = re.compile(r"/(?:articles|doi/(?:abs|full|pdf)?/?)(10\.\d{4,9}/[^\s?#]+)", re.I)
_DOI_ANY = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>,;)]+)")
_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass
class FeedItem:
    """一个 feed 条目，字段已归一。"""

    title: str = ""
    link: str = ""
    summary: str = ""
    doi: str = ""
    published: datetime | None = None
    authors: list[str] = field(default_factory=list)
    venue: str = ""


def _clean(text: str, limit: int = 1200) -> str:
    if not text:
        return ""
    text = _HTML_TAG.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    text = _WS.sub(" ", text).strip()
    if len(text) > limit:
        # ★ 给省略号留位。原项目的真实事故：_clip(text, 60) 没留位，
        #   产出 61 字符撞上 max_length=60 的 pydantic 字段，直接崩。
        text = text[: limit - 1].rstrip() + "…"
    return text


def _entry_doi(entry) -> str:
    for attr in ("prism_doi", "dc_identifier", "doi"):
        value = entry.get(attr)
        if value:
            match = _DOI_ANY.search(str(value))
            if match:
                return match.group(1)
    link = str(entry.get("link") or "")
    match = _DOI_IN_URL.search(link) or _DOI_ANY.search(link)
    return match.group(1) if match else ""


def _entry_summary(entry) -> str:
    """摘要可能藏在四个不同的地方，按信息量从大到小试。"""
    for attr in ("summary", "description"):
        value = entry.get(attr)
        if value:
            return str(value)
    content = entry.get("content")
    if content:
        try:
            return str(content[0].get("value") or "")
        except (IndexError, AttributeError, TypeError):
            pass
    return ""


def _entry_published(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(attr)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    for attr in ("published", "updated", "dc_date", "prism_publicationdate"):
        dt = parse_datetime(entry.get(attr))
        if dt:
            return dt
    return None


def _entry_authors(entry) -> list[str]:
    out: list[str] = []
    author = entry.get("author") or entry.get("dc_creator")
    if author:
        out.append(str(author).strip())
    for a in entry.get("authors") or []:
        name = (a.get("name") or "").strip() if isinstance(a, dict) else str(a).strip()
        if name and name not in out:
            out.append(name)
    return out[:8]


def parse_feed(raw: bytes, *, venue: str = "", limit_chars: int = 1200) -> list[FeedItem]:
    """解析 RSS / Atom / RDF 三种格式。feedparser 三者通吃。"""
    parsed = feedparser.parse(raw)
    items: list[FeedItem] = []
    for entry in parsed.entries:
        title = _clean(str(entry.get("title") or ""), 300)
        link = str(entry.get("link") or "").strip()
        if not title or not link:
            continue
        items.append(
            FeedItem(
                title=title,
                link=link,
                summary=_clean(_entry_summary(entry), limit_chars),
                doi=_entry_doi(entry),
                published=_entry_published(entry),
                authors=_entry_authors(entry),
                venue=venue,
            )
        )
    return items


# --------------------------------------------------------------------------
# 取数
# --------------------------------------------------------------------------


def _http_get_feed(url: str, ua: str | None, timeout_s: float, throttle_s: float) -> bytes:
    from urllib.parse import urlparse

    from .base import throttle_wait

    throttle_wait(urlparse(url).netloc, throttle_s)
    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
    }
    # ★ 三态 UA：none 表示一个 UA 头都不发。
    #   实测有些源「带了 UA 就 403」（syncedreview），有些「不带才 403」（mit_csail）。
    if ua:
        headers["User-Agent"] = ua

    def _once() -> bytes:
        with httpx.Client(timeout=timeout_s, follow_redirects=True, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content

    return retry_call(_once, attempts=3, base_delay=2.0, max_delay=10.0)


def _fetch_bytes(
    url: str, *, key: str, ua: str | None, settings: Settings, offline: bool, ttl_hours: float,
    throttle_s: float = 0.0,
) -> bytes:
    """feed 是二进制，缓存层存的是 JSON，所以这里单独包一层。"""
    cached = cache.read_cache(key)
    if cached is not None and (offline or _fresh(key, ttl_hours)):
        raw = cached.get("b64")
        if raw:
            import base64

            return base64.b64decode(raw)
    if offline:
        raise FileNotFoundError(f"离线模式下缓存里没有 {key}")
    body = _http_get_feed(url, ua, float(settings.api.get("timeout_s", 45.0)), throttle_s)
    import base64

    cache.write_cache(key, {"url": url, "b64": base64.b64encode(body).decode("ascii")})
    return body


def _fresh(key: str, ttl_hours: float) -> bool:
    from .cache import CACHE_DIR, _path_for

    import time as _t

    path = _path_for(key, CACHE_DIR)
    if ttl_hours <= 0 or not path.exists():
        return False
    return (_t.time() - path.stat().st_mtime) / 3600.0 < ttl_hours


# --------------------------------------------------------------------------
# 期刊 feed：同时产出论文与新闻
# --------------------------------------------------------------------------


def fetch_journal_feed(
    journal: Journal, settings: Settings, *, offline: bool = False,
    ttl_hours: float = cache.TTL_HOURS_LOCAL,
) -> tuple[list[Paper], list[NewsItem], SourceReport]:
    """抓一个期刊的 TOC feed。研究论文与新闻稿按 DOI 前缀分流。"""
    report = SourceReport(id=f"rss_{journal.id}", name=f"{journal.name} TOC")
    if not journal.feed:
        report.status = "skipped"
        report.error = "该刊没有配置 feed"
        return [], [], report

    key = cache.cache_key(f"feed_{journal.id}", {"url": journal.feed})
    started = time.monotonic()
    try:
        raw = _fetch_bytes(
            journal.feed, key=key, ua=None, settings=settings,
            offline=offline, ttl_hours=ttl_hours,
        )
        items = parse_feed(raw, venue=journal.name)
    except FileNotFoundError as e:
        report.status = "cached"
        report.error = str(e)
        report.elapsed_s = time.monotonic() - started
        return [], [], report
    except (FetchError, httpx.HTTPStatusError, httpx.TimeoutException) as e:
        report.status = "http_4xx" if "403" in str(e) or "404" in str(e) else "parse_error"
        report.error = str(e)
        report.elapsed_s = time.monotonic() - started
        return [], [], report
    except Exception as e:  # noqa: BLE001 - 单源失败不中止全流程
        report.status = "parse_error"
        report.error = f"{type(e).__name__}: {e}"
        report.elapsed_s = time.monotonic() - started
        return [], [], report

    papers: list[Paper] = []
    news: list[NewsItem] = []
    for item in items:
        if journal.is_research_doi(item.doi) and item.doi:
            papers.append(
                Paper(
                    work_id="",  # feed 给不了 work id，靠 DOI 与 OpenAlex 记录对齐
                    title=item.title,
                    url=item.link,
                    journal=journal.name,
                    issn=journal.issn,
                    tier=journal.tier,
                    discipline=journal.discipline,
                    doi=item.doi,
                    authors=item.authors,
                    published=item.published,
                    abstract=item.summary,
                    venue_type="journal",
                    source_id=f"rss_{journal.id}",
                    source_name=f"{journal.name} TOC",
                    weight=journal.tier_weight,
                )
            )
        else:
            news.append(
                NewsItem(
                    title=item.title,
                    url=item.link,
                    summary=item.summary,
                    source_id=f"rss_{journal.id}",
                    source_name=journal.name,
                    published=item.published,
                    doi=item.doi,
                    venue=journal.name,
                    weight=journal.tier_weight * 0.8,
                )
            )
    report.fetched = len(items)
    report.kept = len(papers) + len(news)
    # ★ 「解析出 0 条」和「这个源今天没内容」是两回事。
    #   实测踩到过：Nature 的 feed 有 139KB / 75 条，某次网络抖动导致
    #   响应被截断，feedparser 安静地返回 0 条 —— 于是体检表上
    #   它显示成 "empty"，看起来像「今天没论文」，实际是取数失败。
    #   拿到体却不解析不出东西，一定是 parse_error。
    if items:
        report.status = "ok"
    elif raw:
        report.status = "parse_error"
        report.error = f"拿到 {len(raw)} 字节但解析出 0 条，响应可能被截断"
    else:
        report.status = "empty"
    report.elapsed_s = time.monotonic() - started
    return papers, news, report


# --------------------------------------------------------------------------
# 独立的科学要闻源
# --------------------------------------------------------------------------


def fetch_news_source(
    source: Source, settings: Settings, *, offline: bool = False,
    ttl_hours: float = cache.TTL_HOURS_LOCAL,
) -> tuple[list[NewsItem], SourceReport]:
    report = SourceReport(id=source.id, name=source.name)
    key = cache.cache_key(f"feed_{source.id}", {"url": source.url})
    started = time.monotonic()
    try:
        raw = _fetch_bytes(
            source.url, key=key, ua=source.ua, settings=settings,
            offline=offline, ttl_hours=ttl_hours, throttle_s=source.throttle_s,
        )
        items = parse_feed(raw, venue=source.name)
    except FileNotFoundError as e:
        report.status = "cached"
        report.error = str(e)
        report.elapsed_s = time.monotonic() - started
        return [], report
    except Exception as e:  # noqa: BLE001
        report.status = "http_4xx" if "403" in str(e) else "parse_error"
        report.error = f"{type(e).__name__}: {e}"
        report.elapsed_s = time.monotonic() - started
        return [], report

    news = [
        NewsItem(
            title=it.title,
            url=it.link,
            summary=it.summary,
            source_id=source.id,
            source_name=source.name,
            published=it.published,
            doi=it.doi,
            venue=it.venue,
            weight=source.weight,
        )
        for it in items
    ]
    report.fetched = len(items)
    report.kept = len(news)
    if news:
        report.status = "ok"
    elif raw:
        report.status = "parse_error"
        report.error = f"拿到 {len(raw)} 字节但解析出 0 条，响应可能被截断"
    else:
        report.status = "empty"
    report.elapsed_s = time.monotonic() - started
    return news, report


def collect_news(
    sources: list[Source], settings: Settings, *, offline: bool = False,
    ttl_hours: float = cache.TTL_HOURS_LOCAL,
) -> tuple[list[NewsItem], list[SourceReport]]:
    """按顺序抓所有新闻源。单个失败只记一行，不抛。"""
    all_items: list[NewsItem] = []
    reports: list[SourceReport] = []
    for source in sources:
        items, report = fetch_news_source(
            source, settings, offline=offline, ttl_hours=ttl_hours
        )
        if not report.ok:
            warn(f"新闻源 {source.name} 失败（{report.status}）：{report.error[:120]}")
        all_items.extend(items)
        reports.append(report)
    return all_items, reports
