"""PubMed E-utilities 适配器。

存在的两个理由：
  1. **摘要是四家里最好的。** Crossref 对 Nature 的摘要基本是 NONE，
     OpenAlex 的 abstract_inverted_index 偶尔缺失；PubMed 的 AbstractText
     是结构化的、几乎总有。
  2. **JAMA 的兜底。** 实测 jamanetwork.com 的 RSS 返回 403，
     所以 JAMA 走 PubMed（0098-7484[ISSN]）。

规矩（NCBI 明文规定，必须遵守）：
    无 API key 时 3 req/s  -> 本模块用 0.34s 串行间隔
    有 API key 时 10 req/s -> 配置 NCBI_API_KEY 后自动提到 0.11s

两段式：esearch 拿 id 列表，efetch 拿详情。这是 E-utilities 的标准用法，
不要试图一次拿全。
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET

from ..settings import Journal, Settings
from . import cache
from .base import FetchError, Paper, SourceReport, http_get_json, parse_datetime

# 摘要字段名归一（PubMed 的 AbstractText 带 Label 属性，如 "BACKGROUND"）
MAX_IDS = 200

# ★ 排除非研究内容。这是**唯一可靠**的办法：
#   实测 JAMA 的新闻稿与人文小品（"Holding Hope"、"Updated Stroke Guideline
#   Emphasizes..."）在 OpenAlex 里 type=article，OpenAlex 的 type_crossref
#   又是空的——两个渠道都分辨不出来。
#   只有 PubMed 的 PublicationType 是权威的。
#   不过滤的话，JAMA 一节会被新闻稿和播客占满。
_NON_RESEARCH_PT = (
    "News",
    "Editorial",
    "Comment",
    "Interview",
    "Biography",
    "Newspaper Article",
    "Congress",
    "Historical Article",
    "Published Erratum",
    "Retraction of Publication",
    "Retracted Publication",
)
_EXCLUDE_PT = " OR ".join(f'"{pt}"[pt]' for pt in _NON_RESEARCH_PT)
_ONLY_NON_RESEARCH = _EXCLUDE_PT


def fetch_non_research_dois(
    journal: Journal, settings: Settings, *, since, until=None,
    offline: bool = False, ttl_hours: float = cache.TTL_HOURS_LOCAL, retmax: int = 500,
) -> set[str]:
    """返回该刊在这段时间里被 PubMed 标为「非研究内容」的 DOI 集合。

    ★ 这是「用 PubMed 给 OpenAlex 排雷」，而不是「用 PubMed 决定留哪些」。

      一开始我写的是后者：只保留 PubMed 返回过的 DOI。结果把 NEJM 的
      正经研究论文全删了——因为 **PubMed 的 [EDAT]（入库日）和
      OpenAlex 的 publication_date（出版日）根本不是一回事**。
      一篇两周前上线的论文，可能几个月前就入库了，于是它落在
      PubMed 的窗口之外，被当成「PubMed 不认识」而删掉。

      反过来做就安全了：只**排除被明确判定为非研究**的那些，
      其余一律保留。判据是权威的（PublicationType），
      窗口错配的代价只是「少排掉几条噪音」，而不是「删掉正经论文」。
    """
    since_s = since.strftime("%Y/%m/%d")
    until_s = (until or _now_dt()).strftime("%Y/%m/%d")
    params = {
        "db": "pubmed",
        "term": f"{journal.issn}[ISSN] AND {since_s}:{until_s}[EDAT] AND ({_ONLY_NON_RESEARCH})",
        "retmax": retmax,
        "retmode": "json",
        **_common_params(settings),
    }
    try:
        search = cache.cached_json(
            cache.cache_key(f"pubmed_nonresearch_{journal.id}", params),
            lambda: http_get_json(
                f"{_base(settings)}/esearch.fcgi",
                params=params,
                timeout_s=float(settings.api.get("timeout_s", 45.0)),
                min_interval_s=_min_interval(settings),
            ),
            ttl_hours=ttl_hours,
            offline=offline,
        )
        ids = ((search or {}).get("esearchresult") or {}).get("idlist") or []
        if not ids:
            return set()
        return _dois_of(ids, journal, settings, offline=offline, ttl_hours=ttl_hours)
    except (FetchError, FileNotFoundError, ET.ParseError):
        # 拿不到名单就不排雷 —— 宁可留几条噪音，也不要因为 PubMed 抖动而改动结果
        return set()


def _dois_of(ids, journal, settings, *, offline, ttl_hours) -> set[str]:
    """取这批 PMID 的 DOI。任何失败都返回空集，绝不抛出。

    ★ 这个函数是「锦上添花」的一步（排掉噪音），它没有资格把整条
      流水线搞挂。所以这里把 httpx 的超时、连接错误全部吞掉——
      实测踩到过：httpx.ReadTimeout 没被捕获，直接冒到顶层让整个运行失败。
    """
    import httpx

    from common.reliability import retry_call

    from .base import throttle_wait

    params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml", **_common_params(settings)}
    key = cache.cache_key(f"pubmed_nonresearch_fetch_{journal.id}", params)
    try:
        cached = cache.read_cache(key)
        if cached is not None and (offline or _fresh(key, ttl_hours)):
            xml_text = cached.get("xml", "")
        elif offline:
            return set()
        else:
            def _once() -> str:
                throttle_wait("eutils.ncbi.nlm.nih.gov", _min_interval(settings))
                with httpx.Client(timeout=float(settings.api.get("timeout_s", 45.0))) as client:
                    resp = client.get(f"{_base(settings)}/efetch.fcgi", params=params)
                    resp.raise_for_status()
                    return resp.text

            xml_text = retry_call(_once, attempts=2, base_delay=1.5, max_delay=6.0)
            cache.write_cache(key, {"xml": xml_text})

        if not xml_text:
            return set()
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001 — 排雷失败就跳过，不影响主流程
        return set()

    out: set[str] = set()
    for article in root.iter("PubmedArticle"):
        for aid in article.iter("ArticleId"):
            if (aid.get("IdType") or "").lower() == "doi":
                doi = (aid.text or "").strip()
                if doi:
                    out.add(doi.lower())
                break
    return out


def _min_interval(settings: Settings) -> float:
    """有 key 10 req/s，无 key 3 req/s。"""
    if os.getenv("NCBI_API_KEY", "").strip():
        return 0.11
    return float(settings.api.get("pubmed_min_interval_s", 0.34))


def _base(settings: Settings) -> str:
    return settings.api.get("pubmed_base", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils")


def _common_params(settings: Settings) -> dict:
    params = {"tool": "scidaily-report", "email": settings.api.get("mailto", "")}
    key = os.getenv("NCBI_API_KEY", "").strip()
    if key:
        params["api_key"] = key
    return params


def fetch_journal(
    journal: Journal, settings: Settings, *, since, until=None,
    offline: bool = False, ttl_hours: float = cache.TTL_HOURS_LOCAL,
) -> tuple[list[Paper], SourceReport]:
    """抓一个医学刊最近入库的论文。

    用 [EDAT]（Entrez 入库日）而不是 [DP]（出版日）：
    日报关心的是「今天新出现的文献」，而出版日往往滞后于入库日数周。
    """
    report = SourceReport(id=f"pubmed_{journal.id}", name=f"{journal.name} (PubMed)")
    since_s = since.strftime("%Y/%m/%d")
    until_s = (until or _now_dt()).strftime("%Y/%m/%d")

    search_params = {
        "db": "pubmed",
        "term": (
            f"{journal.issn}[ISSN] AND {since_s}:{until_s}[EDAT] "
            f"AND NOT ({_EXCLUDE_PT})"
        ),
        "retmax": MAX_IDS,
        "retmode": "json",
        "sort": "date",
        **_common_params(settings),
    }
    started = time.monotonic()
    try:
        search = cache.cached_json(
            cache.cache_key(f"pubmed_search_{journal.id}", search_params),
            lambda: http_get_json(
                f"{_base(settings)}/esearch.fcgi",
                params=search_params,
                timeout_s=float(settings.api.get("timeout_s", 45.0)),
                min_interval_s=_min_interval(settings),
            ),
            ttl_hours=ttl_hours,
            offline=offline,
        )
        ids = ((search or {}).get("esearchresult") or {}).get("idlist") or []
        if not ids:
            report.status = "empty"
            report.elapsed_s = time.monotonic() - started
            return [], report

        papers = _fetch_details(ids, journal, settings, offline=offline, ttl_hours=ttl_hours)
    except (FetchError, FileNotFoundError) as e:
        # ★ FileNotFoundError = 离线且无缓存；这不算故障，算「这个源今天没数据」
        report.status = "cached" if isinstance(e, FileNotFoundError) else "parse_error"
        report.error = str(e)
        report.elapsed_s = time.monotonic() - started
        return [], report
    except ET.ParseError as e:
        report.status = "parse_error"
        report.error = f"XML 解析失败：{e}"
        report.elapsed_s = time.monotonic() - started
        return [], report

    report.fetched = len(ids)
    report.kept = len(papers)
    report.status = "ok" if papers else "empty"
    report.elapsed_s = time.monotonic() - started
    return papers, report


def _fetch_details(
    ids: list[str], journal: Journal, settings: Settings, *, offline: bool, ttl_hours: float
) -> list[Paper]:
    """efetch 拿 XML 详情，再解析出摘要与 DOI。

    E-utilities 的 efetch 不返回 JSON（retmode=json 对部分库无效），
    所以这里用 http_get_json 的兄弟——直接拿文本。
    """
    import httpx

    params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "xml",
        **_common_params(settings),
    }
    key = cache.cache_key(f"pubmed_fetch_{journal.id}", params)

    cached = cache.read_cache(key)
    if cached is not None and (offline or _fresh(key, ttl_hours)):
        xml_text = cached.get("xml", "")
    elif offline:
        raise FileNotFoundError(f"离线模式下没有 {key}")
    else:
        from .base import throttle_wait

        throttle_wait("eutils.ncbi.nlm.nih.gov", _min_interval(settings))
        with httpx.Client(timeout=float(settings.api.get("timeout_s", 45.0))) as client:
            resp = client.get(f"{_base(settings)}/efetch.fcgi", params=params)
            resp.raise_for_status()
            xml_text = resp.text
        cache.write_cache(key, {"xml": xml_text})

    if not xml_text:
        return []
    return _parse_pubmed_xml(xml_text, journal)


def _parse_pubmed_xml(xml_text: str, journal: Journal) -> list[Paper]:
    root = ET.fromstring(xml_text)
    out: list[Paper] = []
    for article in root.iter("PubmedArticle"):
        pmid = _text(article.find(".//PMID"))
        title = _text(article.find(".//ArticleTitle"))
        if not title:
            continue

        # ★ 权威的非研究内容过滤，在 XML 里做，不依赖 esearch 的 NOT 语义。
        #   实测：`AND NOT ("Editorial"[pt])` 能砍掉大部分（47→19），
        #   但**仍有社论漏过来**——实测 DOI 10.1056/nejme2611312 的
        #   PublicationType 明明就是 "Editorial"，esearch 却没排除它。
        #   所以 esearch 的那层过滤只当作省带宽的粗筛，
        #   真正的判据在这里：解析出来的 PublicationType 列表。
        if _is_non_research(article):
            continue

        abstract = _abstract(article)
        doi = ""
        for aid in article.iter("ArticleId"):
            if (aid.get("IdType") or "").lower() == "doi":
                doi = (aid.text or "").strip()
                break

        published = _pub_date(article)
        authors = [
            _text(a.find("LastName")) or _text(a.find("CollectiveName"))
            for a in article.iter("Author")
        ]
        authors = [a for a in authors if a][:8]

        out.append(
            Paper(
                work_id="",  # 靠 DOI 与 OpenAlex 记录对齐（merge.resolve_work_ids）
                title=title,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                journal=journal.name,
                issn=journal.issn,
                tier=journal.tier,
                discipline=journal.discipline,
                doi=doi,
                authors=authors,
                published=published,
                abstract=abstract,
                venue_type="journal",
                source_id=f"pubmed_{journal.id}",
                source_name=f"{journal.name} (PubMed)",
                weight=journal.tier_weight,
            )
        )
    return out


def _is_non_research(article) -> bool:
    """该条目是否属于非研究内容（社论 / 新闻 / 评论 / 播客 …）。

    判据是「PublicationType 里出现了任何一个非研究类型」。
    实测站得住：NEJM 的社论只挂 "Editorial" 一个类型。
    """
    types = {(_text(pt) or "").strip().lower() for pt in article.iter("PublicationType")}
    types.discard("")
    if not types:
        return False       # 没有类型信息时不擅自丢弃
    blocked = {pt.lower() for pt in _NON_RESEARCH_PT}
    return bool(types & blocked)


def _abstract(article) -> str:
    """把带 Label 的 AbstractText 拼成一段。

    PubMed 的结构化摘要长这样：
        <AbstractText Label="BACKGROUND">...</AbstractText>
        <AbstractText Label="METHODS">...</AbstractText>
    直接拼会丢掉结构，所以保留 Label 前缀。
    """
    parts: list[str] = []
    for node in article.iter("AbstractText"):
        text = "".join(node.itertext()).strip()
        if not text:
            continue
        label = (node.get("Label") or "").strip()
        parts.append(f"{label}: {text}" if label else text)
    return " ".join(parts)[:1600]


def _pub_date(article) -> "object":
    node = article.find(".//ArticleDate")
    if node is None:
        node = article.find(".//JournalIssue/PubDate")
    if node is None:
        return None
    year = _text(node.find("Year")) or _text(node.find("MedlineDate"))[:4]
    month = _text(node.find("Month")) or "01"
    day = _text(node.find("Day")) or "01"
    return parse_datetime(f"{year}-{_month_num(month)}-{day.zfill(2)}")


_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _month_num(month: str) -> str:
    month = (month or "").strip()
    if month.isdigit():
        return month.zfill(2)
    return _MONTHS.get(month[:3].lower(), "01")


def _text(node) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def _fresh(key: str, ttl_hours: float) -> bool:
    from .cache import CACHE_DIR, _path_for

    path = _path_for(key, CACHE_DIR)
    if ttl_hours <= 0 or not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) / 3600.0 < ttl_hours


def _now_dt():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
