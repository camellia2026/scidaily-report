"""采集层的公共类型与礼貌原语。

三件东西在这里定义一次，所有适配器共用：
  1. 记录模型（Paper / NewsItem）
  2. 源健康报告（SourceReport）—— 单源失败永不中止整条流水线
  3. 逐 host 节流 + 带重试的 JSON 取数

★ 节流与礼貌池是从零写的。原项目的 common/reliability.py 只提供 retry_call
  （传输无关的指数退避），既没有 http 助手，也没有 User-Agent 管理，
  更没有速率闸门；而学术 API 恰恰在这三件事上各有各的规矩。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from common.console import warn
from common.reliability import retry_call

# --------------------------------------------------------------------------
# 记录模型
# --------------------------------------------------------------------------


@dataclass
class Paper:
    """一篇被追踪的学术文献。

    work_id 是合并与去重的锚点：OpenAlex 把同一篇论文的预印本版与期刊版
    聚在同一个 work 下，所以按 work_id 去重 = 增强点「预印本→正式发表合并」
    自动成立。
    """

    work_id: str
    title: str
    url: str
    journal: str = ""
    issn: str = ""
    tier: str = "T2"
    discipline: str = "综合"
    doi: str = ""
    authors: list[str] = field(default_factory=list)
    published: datetime | None = None
    citation_count: int = 0
    cites_per_year: float = 0.0
    counts_by_year: dict[int, int] = field(default_factory=dict)
    # ★ counts_by_year 是识别「真热文」与「老名篇」的关键，不是装饰。
    #   实测反例：热文候选榜首是 JAMA 重新发表的《赫尔辛基宣言》，
    #   总引用 1523、篇均年引用 780——按总量排它永远第一。
    #   但它的引用摊在几十年里（近年占比 ~3%），而一篇上线半年的论文
    #   有 80 次引用、近年占比 ~100%。**「近年引用占比」才是分界线**，
    #   只看总量会让报告天天出现同一批老文献。
    recent_citations: int | None = None  # 近 90 天被引，Tier B 才填
    abstract: str = ""
    venue_type: str = "journal"  # journal | preprint
    oa_url: str = ""
    is_oa: bool = False
    locations: list[dict] = field(default_factory=list)
    source_id: str = ""
    source_name: str = ""
    weight: float = 1.0
    kind: str = "paper"

    @property
    def age_hours(self) -> float | None:
        if self.published is None:
            return None
        return (datetime.now(timezone.utc) - self.published).total_seconds() / 3600.0

    @property
    def age_years(self) -> float:
        h = self.age_hours
        return (h / 8766.0) if h is not None else 1.0

    def to_dict(self) -> dict:
        return {
            "work_id": self.work_id,
            "kind": self.kind,
            "title": self.title,
            "url": self.url,
            "doi": self.doi,
            "journal": self.journal,
            "issn": self.issn,
            "tier": self.tier,
            "discipline": self.discipline,
            "authors": list(self.authors),
            "published": self.published.isoformat() if self.published else None,
            "citation_count": self.citation_count,
            "cites_per_year": round(self.cites_per_year, 3),
            "counts_by_year": {str(k): v for k, v in self.counts_by_year.items()},
            "recent_citations": self.recent_citations,
            "abstract": self.abstract,
            "venue_type": self.venue_type,
            "oa_url": self.oa_url,
            "is_oa": self.is_oa,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "weight": self.weight,
        }

    @staticmethod
    def from_dict(raw: dict) -> "Paper":
        published = raw.get("published")
        return Paper(
            work_id=str(raw.get("work_id", "")),
            title=str(raw.get("title", "")),
            url=str(raw.get("url", "")),
            journal=str(raw.get("journal", "")),
            issn=str(raw.get("issn", "")),
            tier=str(raw.get("tier", "T2")),
            discipline=str(raw.get("discipline", "综合")),
            doi=str(raw.get("doi", "")),
            authors=list(raw.get("authors") or []),
            published=datetime.fromisoformat(published) if published else None,
            citation_count=int(raw.get("citation_count", 0) or 0),
            cites_per_year=float(raw.get("cites_per_year", 0.0) or 0.0),
            counts_by_year={
                int(k): int(v) for k, v in (raw.get("counts_by_year") or {}).items()
            },
            recent_citations=raw.get("recent_citations"),
            abstract=str(raw.get("abstract", "")),
            venue_type=str(raw.get("venue_type", "journal")),
            oa_url=str(raw.get("oa_url", "")),
            is_oa=bool(raw.get("is_oa", False)),
            source_id=str(raw.get("source_id", "")),
            source_name=str(raw.get("source_name", "")),
            weight=float(raw.get("weight", 1.0) or 1.0),
        )


@dataclass
class NewsItem:
    """一条科学要闻（期刊新闻版 / 科学媒体）。"""

    title: str
    url: str
    summary: str = ""
    source_id: str = ""
    source_name: str = ""
    published: datetime | None = None
    weight: float = 1.0
    doi: str = ""
    venue: str = ""
    kind: str = "news"

    @property
    def age_hours(self) -> float | None:
        if self.published is None:
            return None
        return (datetime.now(timezone.utc) - self.published).total_seconds() / 3600.0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "published": self.published.isoformat() if self.published else None,
            "doi": self.doi,
            "venue": self.venue,
            "weight": self.weight,
        }

    @staticmethod
    def from_dict(raw: dict) -> "NewsItem":
        published = raw.get("published")
        return NewsItem(
            title=str(raw.get("title", "")),
            url=str(raw.get("url", "")),
            summary=str(raw.get("summary", "")),
            source_id=str(raw.get("source_id", "")),
            source_name=str(raw.get("source_name", "")),
            published=datetime.fromisoformat(published) if published else None,
            weight=float(raw.get("weight", 1.0) or 1.0),
            doi=str(raw.get("doi", "")),
            venue=str(raw.get("venue", "")),
        )


Record = Paper | NewsItem


# --------------------------------------------------------------------------
# 源健康
# --------------------------------------------------------------------------


@dataclass
class SourceReport:
    """一个采集单元（一个期刊的某条通道，或一个 RSS 源）的健康状况。

    ★ 单源失败永远不中止整条流水线——它只是变成报告末尾表格里的一行。
      唯一的硬失败是「所有源都挂了」，那由 main.py 判定。
    """

    id: str
    name: str
    status: str = "ok"
    # ok | http_4xx | http_5xx | timeout | parse_error | empty | cached | skipped
    fetched: int = 0
    kept: int = 0
    elapsed_s: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "cached", "empty")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "fetched": self.fetched,
            "kept": self.kept,
            "elapsed_s": round(self.elapsed_s, 2),
            "error": self.error,
        }


class FetchError(Exception):
    """取数失败。适配器把它转成 SourceReport，不往上抛。"""


# --------------------------------------------------------------------------
# 逐 host 节流
# --------------------------------------------------------------------------

_throttle_lock = threading.Lock()
_last_call: dict[str, float] = {}


def throttle_wait(host: str, min_interval: float) -> float:
    """确保对同一 host 的两次请求间隔 >= min_interval 秒。

    各家的规矩不一样，且都是明文写着的，所以必须逐 host 配置：
        PubMed E-utilities  无 key 时 3 req/s  -> 0.34s
        arXiv               官方要求 >= 3s     -> 3.0s
        OpenAlex/Crossref  带 mailto 进礼貌池，无需硬节流
    """
    if min_interval <= 0:
        return 0.0
    with _throttle_lock:
        last = _last_call.get(host, 0.0)
        wait = min_interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()
        return max(wait, 0.0)


def reset_throttle() -> None:
    """测试用：清空节流记账，避免用例之间互相拖慢。"""
    with _throttle_lock:
        _last_call.clear()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_JSON_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "scidaily-report/0.1 (mailto:see SCIDAILY_CONTACT)",
}


def http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout_s: float = 45.0,
    min_interval_s: float = 0.0,
    attempts: int = 3,
    on_retry=None,
) -> Any:
    """带节流与重试的 JSON 取数。

    retry_call 来自 common/reliability.py，只对 transient 类错误重试
    （429/5xx/超时）；4xx 是 fatal，立即抛出——重试一个 404 只是浪费三倍时间。
    """
    host = urlparse(url).netloc
    throttle_wait(host, min_interval_s)

    def _once() -> Any:
        with httpx.Client(timeout=timeout_s, follow_redirects=True, headers=_JSON_HEADERS) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    try:
        return retry_call(_once, attempts=attempts, base_delay=2.0, max_delay=10.0, on_retry=on_retry)
    except httpx.HTTPStatusError as e:
        raise FetchError(f"HTTP {e.response.status_code} {url}") from e
    except httpx.TimeoutException as e:
        raise FetchError(f"超时 {url}") from e
    except json.JSONDecodeError as e:
        raise FetchError(f"响应不是 JSON {url}: {e}") from e
    except Exception as e:  # noqa: BLE001 - 适配器统一降级为 SourceReport
        raise FetchError(f"{type(e).__name__}: {e}") from e


# --------------------------------------------------------------------------
# OpenAlex 摘要还原
# --------------------------------------------------------------------------


def decode_abstract_inverted_index(index: dict | None, *, max_chars: int = 1600) -> str:
    """把 OpenAlex 的 abstract_inverted_index 还原成可读文本。

    OpenAlex 为省带宽，把摘要存成 {词: [出现位置...]}。还原就是反着铺一遍。

    ★ 两个必须处理的边界：
      - index 为 None（社论、通讯、勘误类常常没有摘要）-> 返回空串，不抛。
      - 位置序列可能不连续（OpenAlex 偶尔缺词），所以要按最大位置开数组，
        而不是假定 len == 词数。
    """
    if not index:
        return ""
    try:
        positions: list[tuple[int, str]] = []
        for word, idxs in index.items():
            for i in idxs:
                positions.append((int(i), word))
    except (AttributeError, TypeError, ValueError):
        return ""
    if not positions:
        return ""
    positions.sort(key=lambda p: p[0])
    text = " ".join(word for _, word in positions)
    if len(text) > max_chars:
        cut = text[:max_chars]
        # 在词边界上收尾，免得截出半个词
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        text = cut.rstrip() + " …"
    return text


def parse_datetime(value: str | None) -> datetime | None:
    """解析各家 API 返回的日期/时间，统一成带时区的 datetime。

    接受：2026-09-23 / 2026-09-23T10:11:12Z / 2026-09-23T10:11:12+00:00
    失败返回 None（当作「日期未知」处理，由 keep_undated 决定去留）。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def warn_source(report: SourceReport) -> None:
    if not report.ok:
        warn(f"源 {report.name} 失败（{report.status}）：{report.error[:160]}")
