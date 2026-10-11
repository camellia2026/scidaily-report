"""OpenAlex 适配器 —— 本项目的主力采集通道。

为什么是主力（三点，均为 2026-10-01 本机实测）：

1. **一次请求覆盖全部期刊。** filter 里 ISSN 支持管道（OR）：
       primary_location.source.issn:0028-0836|0162-8828|...
   实测一次拿回 132 条、响应仅 7KB。整个每日论文扫描基本就是一个请求。

2. **它已经把预印本与正式发表版聚在同一个 work。** 一篇 Nature 论文的
   `locations` 里同时有 Nature(publishedVersion)、PubMed、PubMed Central、
   机构库(submittedVersion)。→ 增强点「预印本→正式发表合并」天然成立，
   只要按 work id 去重即可，不需要事后做模糊匹配。

3. **限流显式且宽裕。** 响应头 X-RateLimit-Limit: 1000（信用点/天），
   单次约 0.0001 USD。本设计约 20 次/天，占 2%。带 mailto 进礼貌池。

它也补上了 Crossref 的两个洞：JMLR 在 Crossref 里 total=0，但在 OpenAlex
有 1298 篇；Acta Numerica 等年刊按名可查。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from ..settings import Journal, Settings
from . import cache
from .base import (
    FetchError,
    Paper,
    SourceReport,
    decode_abstract_inverted_index,
    http_get_json,
    parse_datetime,
)

# arXiv 在 OpenAlex 里的 source id（实测）。
# 用于把预印本也走 OpenAlex 取——这样预印本与正式版共享 work_id，
# 合并就是「从一开始就别拆」，而不是事后拼。
ARXIV_SOURCE_ID = "https://openalex.org/S4306400194"

MAX_PER_PAGE = 200

# 预印本通道只接受这两种 type。
# ★ 为什么不写「排除 book-chapter」而是写「只允许这两个」：
#   白名单在遇到没想到的类型时会**漏**（安全的方向），
#   黑名单在遇到没想到的类型时会**错收**（危险的方向）。
#   一条被漏掉的预印本只是少看一篇；一条被错收的电子书章节
#   会以「预印本」的名义出现在报告里，是更糟的失败。
PREPRINT_TYPES = ("preprint", "article")

# 只取需要的字段。不裁剪的话单条 work 的 JSON 会大出一个数量级。
SELECT = ",".join(
    [
        "id",
        "doi",
        "title",
        "display_name",
        "publication_date",
        "publication_year",
        "type",
        "primary_location",
        "locations",
        "authorships",
        "cited_by_count",
        "counts_by_year",
        "abstract_inverted_index",
        "open_access",
        "is_retracted",
        # 用于识别「根本不是研究内容」的条目（新闻稿、人文小品）
        "primary_topic",
        "referenced_works_count",
    ]
)


@dataclass
class _Work:
    """一行 OpenAlex works 结果。保留原始 dict 以便 merge.py 复判。"""

    raw: dict

    @property
    def id(self) -> str:
        return str(self.raw.get("id") or "")

    @property
    def doi(self) -> str:
        return str(self.raw.get("doi") or "")

    @property
    def title(self) -> str:
        return str(self.raw.get("title") or self.raw.get("display_name") or "")

    @property
    def published(self) -> datetime | None:
        return parse_datetime(self.raw.get("publication_date"))

    @property
    def citations(self) -> int:
        return int(self.raw.get("cited_by_count") or 0)

    @property
    def locations(self) -> list[dict]:
        return self.raw.get("locations") or []

    @property
    def type(self) -> str:
        return str(self.raw.get("type") or "article")

    @property
    def is_retracted(self) -> bool:
        return bool(self.raw.get("is_retracted"))


# --------------------------------------------------------------------------
# 期刊查询
# --------------------------------------------------------------------------


def _issn_filter(issns: Iterable[str]) -> str:
    """管道 = OR。这是「一个请求覆盖全部期刊」的实现。"""
    return "|".join(sorted({i for i in issns if i}))


def _page_through(
    name: str,
    base_params: dict,
    settings: Settings,
    *,
    offline: bool,
    ttl_hours: float,
    max_pages: int,
) -> list[dict]:
    """按 cursor 翻页，把全部结果聚成一个列表再缓存。

    为什么要翻页：per-page 上限是 200，而 12 个期刊 14 天的窗口实测能到 200+。
    不翻页的话超出的部分会被静默丢掉——那是最难发现的一类 bug
    （报告看起来正常，只是少了一些论文）。

    缓存的是**聚合后的结果**而不是单页：cursor 是不透明令牌，
    按页缓存会得到一堆难以复用的碎片。
    """
    url = f"{settings.api['openalex_base']}/works"
    timeout = float(settings.api.get("timeout_s", 45.0))

    def _do() -> dict:
        results: list[dict] = []
        cursor = "*"
        for _ in range(max_pages):
            params = dict(base_params, cursor=cursor)
            payload = http_get_json(url, params=params, timeout_s=timeout, min_interval_s=0.0)
            page = payload.get("results") or []
            results.extend(page)
            cursor = (payload.get("meta") or {}).get("next_cursor")
            # 拿不满一页 = 已经到底
            if not cursor or len(page) < MAX_PER_PAGE:
                break
        return {"results": results}

    payload = cache.cached_json(
        cache.cache_key(name, base_params),
        _do,
        ttl_hours=ttl_hours,
        offline=offline,
    )
    return (payload or {}).get("results") or []


def fetch_journals(
    journals: list[Journal],
    settings: Settings,
    *,
    since: datetime,
    offline: bool = False,
    ttl_hours: float = cache.TTL_HOURS_LOCAL,
    max_pages: int = 3,
) -> tuple[list[Paper], SourceReport]:
    """抓取所有被追踪期刊在 since 之后上线的论文。"""
    issns = [j.issn for j in journals if "openalex" in j.route]
    report = SourceReport(id="openalex_journals", name="OpenAlex · 期刊")
    if not issns:
        report.status = "skipped"
        report.error = "没有任何期刊启用 openalex 通道"
        return [], report

    by_issn = {j.issn: j for j in journals}
    params = {
        "filter": (
            f"locations.source.issn:{_issn_filter(issns)},"
            f"from_publication_date:{since.strftime('%Y-%m-%d')}"
        ),
        "per-page": MAX_PER_PAGE,
        "select": SELECT,
        "mailto": settings.api.get("mailto", ""),
    }

    started = _now()
    try:
        results = _page_through(
            "openalex_journals", params, settings,
            offline=offline, ttl_hours=ttl_hours, max_pages=max_pages,
        )
    except (FetchError, FileNotFoundError) as e:
        report.status = "cached" if isinstance(e, FileNotFoundError) else "parse_error"
        report.error = str(e)
        report.elapsed_s = _now() - started
        return [], report

    report.fetched = len(results)
    papers: list[Paper] = []
    for raw in results:
        w = _Work(raw)
        if w.is_retracted or not w.title:
            continue
        paper = _to_paper(w, by_issn, settings)
        if paper is not None:
            papers.append(paper)
    report.kept = len(papers)
    report.status = "ok" if papers else "empty"
    report.elapsed_s = _now() - started
    return papers, report


def fetch_notable_preprints(
    settings: Settings,
    *,
    days: int = 60,
    min_citations: int = 5,
    limit: int = 60,
    offline: bool = False,
    ttl_hours: float = cache.TTL_HOURS_LOCAL,
) -> tuple[list[Paper], SourceReport]:
    """抓「值得看的」arXiv 预印本，而不是全部预印本。

    ★ 为什么不能抓全部：arXiv 每天约 3800 篇（实测 09-28 单日 2465，之前一天 1028）。
      全量灌进来只会把报告淹掉——这正是原项目「arXiv 单日 596 条共用一个
      pubDate，24 小时窗口完全失效」那条教训的放大版。

    ★ 为什么用「引用数阈值」而不是「最新」：
      (a) 它天然把 3800 篇砍到约 100 篇（实测 cited_by_count:>4 + 近 60 天 = 98 篇）；
      (b) 一篇上线两周就被引 10 次的预印本，比昨天上线、无人问津的那篇更值得报；
      (c) 它绕开了 OpenAlex 对 arXiv 的收录滞后——实测 09-28 有 2465 篇、
          09-30 只剩 275 篇、10-01 只有 12 篇。等它攒够引用，索引也早就跟上了。

      预印本的「未经过同行评审」身份会在报告里明确标注。
    """
    report = SourceReport(id="openalex_preprints", name="OpenAlex · arXiv 重点预印本")
    params = {
        "filter": (
            f"locations.source.id:{ARXIV_SOURCE_ID},"
            f"from_publication_date:{_days_ago(days)},cited_by_count:>{min_citations - 1}"
        ),
        "sort": "cited_by_count:desc",
        "per-page": min(limit, MAX_PER_PAGE),
        "select": SELECT,
        "mailto": settings.api.get("mailto", ""),
    }

    started = _now()
    try:
        results = _page_through(
            "openalex_notable_preprints", params, settings,
            offline=offline, ttl_hours=ttl_hours, max_pages=1,
        )
    except (FetchError, FileNotFoundError) as e:
        report.status = "cached" if isinstance(e, FileNotFoundError) else "parse_error"
        report.error = str(e)
        report.elapsed_s = _now() - started
        return [], report

    report.fetched = len(results)
    papers: list[Paper] = []
    for raw in results:
        w = _Work(raw)
        if w.is_retracted or not w.title:
            continue
        paper = _to_paper(w, {}, settings)
        if paper is None:
            continue
        # ★ 只留**真预印本**。两道关：
        #   (a) venue_type == "preprint"：排除主发布位置是期刊的 work。
        #       过滤条件 `locations.source.id:arXiv` 会命中一大类 work：
        #       它们确实有 arXiv 版本，但主发布位置是某个期刊
        #       （实测遇到 PLoS ONE、Journal of Operator Theory 等）。
        #       这些是「已发表的论文」，标成预印本是错的。
        #   (b) 必须真的有一个**仓储类** location（arXiv / 机构库）。
        #       只靠 (a) 不够：实测漏进来过 "ANU Press eBooks" ——
        #       电子书源的 type 不是 journal，于是被判成 preprint。
        #   (c) type 必须真的是论文。实测漏进来过 "ANU Press eBooks" 的
        #       book-chapter：它同时挂着 arXiv 与 RePEc 两个 repository
        #       location，前两道关都拦不住。判据只能是 work 自己的 type。
        if paper.venue_type != "preprint" or not _has_repository_location(w):
            continue
        if w.type not in PREPRINT_TYPES:
            continue
        papers.append(paper)
    report.kept = len(papers)
    report.status = "ok" if papers else "empty"
    report.elapsed_s = _now() - started
    return papers, report


def fetch_hot_journal_papers(
    journals: list[Journal],
    settings: Settings,
    *,
    months: int = 24,
    limit: int = 60,
    offline: bool = False,
    ttl_hours: float = cache.TTL_HOURS_LOCAL,
) -> tuple[list[Paper], SourceReport]:
    """抓「近两年内、被引最多」的期刊论文，作为热文通道的输入。

    ★ 为什么要单独查一次、而不是复用每日那批：
      每日窗口只有 72 小时，而热文定义上是「发表一段时间后才显出发热」的论文。
      两者是**互斥的集合**，不能互相替代。

    ★ 为什么限定 24 个月：
      没有年龄上限的话，历年高引论文会永远霸榜——那是「著名」，不是「热」。
      年龄上限才是「热文」与「名篇」的分界线。

    排序用 cited_by_count 只是粗筛（省请求）；真正的速度指标由 velocity.py
    用 counts_by_year 与 Tier B 精查算出来。
    """
    issns = [j.issn for j in journals if "openalex" in j.route]
    report = SourceReport(id="openalex_hot", name="OpenAlex · 热文候选")
    if not issns:
        report.status = "skipped"
        report.error = "没有任何期刊启用 openalex 通道"
        return [], report

    by_issn = {j.issn: j for j in journals}
    params = {
        "filter": (
            f"locations.source.issn:{_issn_filter(issns)},"
            f"from_publication_date:{_months_ago(months)},type:article"
        ),
        "sort": "cited_by_count:desc",
        "per-page": min(limit, MAX_PER_PAGE),
        "select": SELECT,
        "mailto": settings.api.get("mailto", ""),
    }

    started = _now()
    try:
        results = _page_through(
            "openalex_hot", params, settings,
            offline=offline, ttl_hours=ttl_hours, max_pages=1,
        )
    except (FetchError, FileNotFoundError) as e:
        report.status = "cached" if isinstance(e, FileNotFoundError) else "parse_error"
        report.error = str(e)
        report.elapsed_s = _now() - started
        return [], report

    report.fetched = len(results)
    papers: list[Paper] = []
    for raw in results:
        w = _Work(raw)
        if w.is_retracted or not w.title:
            continue
        paper = _to_paper(w, by_issn, settings)
        if paper is not None:
            papers.append(paper)
    report.kept = len(papers)
    report.status = "ok" if papers else "empty"
    report.elapsed_s = _now() - started
    return papers, report


def _days_ago(days: int) -> str:
    from datetime import timedelta

    return (datetime.now(tz=None) - timedelta(days=days)).strftime("%Y-%m-%d")


def _months_ago(months: int) -> str:
    from datetime import timedelta

    return (datetime.now(tz=None) - timedelta(days=months * 31)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# 引用速度（增强 4 的 Tier B）
# --------------------------------------------------------------------------


def count_recent_citations(
    work_id: str, since: datetime, settings: Settings, *, offline: bool = False
) -> int | None:
    """精确统计某篇论文在 since 之后被引的次数。

    这是 Tier B：counts_by_year 只给年度粒度，要「近 90 天」就必须真去数
    引用它的那些 work 的出版日期。代价是每篇 1 个请求，所以只对短名单做。

    只读 meta.count，不拉取实际条目——响应体因此非常小。
    """
    if not work_id:
        return None
    short = work_id.rsplit("/", 1)[-1]
    params = {
        "filter": f"cites:{short},from_publication_date:{since.strftime('%Y-%m-%d')}",
        "per-page": 1,
        "select": "id",
        "mailto": settings.api.get("mailto", ""),
    }
    key = cache.cache_key(f"openalex_cites_{short}", params)
    try:
        payload = cache.cached_json(
            key,
            lambda: http_get_json(
                f"{settings.api['openalex_base']}/works",
                params=params,
                timeout_s=float(settings.api.get("timeout_s", 45.0)),
            ),
            ttl_hours=cache.TTL_HOURS_LOCAL,
            offline=offline,
        )
    except (FetchError, FileNotFoundError):
        return None
    meta = (payload or {}).get("meta") or {}
    count = meta.get("count")
    return int(count) if count is not None else None


# --------------------------------------------------------------------------
# 映射
# --------------------------------------------------------------------------


def _to_paper(work: _Work, by_issn: dict[str, Journal], settings: Settings) -> Paper | None:
    """把一个 OpenAlex work 映射成 Paper，并判定它是期刊版还是预印本版。

    ★ 判定顺序是有意的：先找**期刊 location**（不管它是不是 primary）。
      因为一篇论文的 primary_location 可能是 arXiv，但它已经正式发表在
      Nature 上——那种情况下它应该以期刊论文的身份出现，而不是预印本。
    """
    journal, issn, venue_type = _resolve_venue(work, by_issn)

    title = work.title.strip()
    if not _is_plausible_title(title):
        return None
    # 标题就是刊名 —— 实测遇到过 title="JAMA" 的刊级记录混在论文里
    if journal is not None and title.strip().lower() == journal.name.strip().lower():
        return None
    if _looks_like_non_research(work):
        return None

    primary = work.raw.get("primary_location") or {}
    oa = work.raw.get("open_access") or {}
    url = (
        primary.get("landing_page_url")
        or (f"https://doi.org/{_strip_doi(work.doi)}" if work.doi else "")
        or oa.get("oa_url")
        or work.id
    )

    authors = []
    for a in (work.raw.get("authorships") or [])[:8]:
        name = ((a.get("author") or {}).get("display_name") or "").strip()
        if name:
            authors.append(name)

    return Paper(
        work_id=work.id,
        title=title,
        url=url,
        journal=journal.name if journal else _source_name_of(primary) or "arXiv",
        issn=issn,
        tier=journal.tier if journal else "T2",
        discipline=journal.discipline if journal else "综合",
        doi=_strip_doi(work.doi),
        authors=authors,
        published=work.published,
        citation_count=work.citations,
        cites_per_year=_cites_per_year(work.citations, work.published),
        counts_by_year=_counts_by_year(work.raw.get("counts_by_year")),
        abstract=decode_abstract_inverted_index(work.raw.get("abstract_inverted_index")),
        venue_type=venue_type,
        oa_url=str(oa.get("oa_url") or ""),
        is_oa=bool(oa.get("is_oa")),
        locations=work.locations,
        source_id="openalex",
        source_name="OpenAlex",
        weight=journal.tier_weight if journal else 1.0,
    )


def _resolve_venue(
    work: _Work, by_issn: dict[str, Journal]
) -> tuple[Journal | None, str, str]:
    """返回 (期刊, issn, venue_type)。

    ★ 必须同时看 `issn_l` 和 `issn` 数组，这是实测踩出来的：
      OpenAlex 的 `issn_l` 是「linking ISSN」，**不等于**通常引用的印刷版 ISSN。
      实测：JAMA 的 issn_l 是 0002-9955（而印刷版是 0098-7484），
            The Lancet 的 issn_l 是 0099-5355（而印刷版是 0140-6736）。
      只比 issn_l 的话，这两个 T0 医学刊的论文会被判成「未追踪期刊」，
      降级成 T2/综合 —— 分级权重和学科分区同时失效，而且不会报错。

      所以：`issn_l` 先试（最快），不中再扫 `issn` 数组。
    """
    for loc in work.locations:
        source = loc.get("source") or {}
        journal = _match_journal(source, by_issn)
        if journal is not None:
            return journal, journal.issn, "journal"

    # 没命中被追踪的期刊：看 primary location 是不是期刊类源
    primary_source = (work.raw.get("primary_location") or {}).get("source") or {}
    if (primary_source.get("type") or "") == "journal":
        # 是期刊但不在我们的清单里 —— 仍然算「已正式发表」，
        # 因为它的预印本身份已经结束了。
        return None, str(primary_source.get("issn_l") or ""), "journal"
    return None, "", "preprint"


def _match_journal(source: dict, by_issn: dict[str, Journal]) -> Journal | None:
    issn_l = str(source.get("issn_l") or "").strip()
    if issn_l and issn_l in by_issn:
        return by_issn[issn_l]
    for candidate in source.get("issn") or []:
        journal = by_issn.get(str(candidate).strip())
        if journal is not None:
            return journal
    return None


def _source_name_of(location: dict) -> str:
    return str(((location or {}).get("source") or {}).get("display_name") or "")


def _strip_doi(doi: str) -> str:
    doi = (doi or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            return doi[len(prefix):]
    return doi


def _looks_like_non_research(work: _Work) -> bool:
    """该 work 是否完全没有学术信号——即「不是研究内容」。

    三缺一不可，全是**缺席**型判据，所以不会误伤内容丰富的正经论文：
        无摘要  且  无主题  且  无参考文献

    实测依据：JAMA 的 "Holding Hope"、"The Mayor" 三项全空；
    而一篇摘要尚未生成的**新研究论文**至少有主题或参考文献，不会被误删。
    （对照：OpenAlex 对 JAMA/NEJM 近 14 天 108 条里，三项全空的只有 3 条。）

    ★ 明确的能力边界：这条规则抓得住「完全不像论文」的条目，
      但抓不住 JAMA 的新闻稿——那些有主题、只是没有摘要
      （实测 "Updated Stroke Guideline Emphasizes..."）。
      那类残余噪音交给 LLM 那一层判断，规则层不硬猜。
    """
    has_abstract = bool(work.raw.get("abstract_inverted_index"))
    has_topic = bool(work.raw.get("primary_topic"))
    n_refs = int(work.raw.get("referenced_works_count") or 0)
    return not has_abstract and not has_topic and n_refs == 0


def _has_repository_location(work: _Work) -> bool:
    """该 work 是否真的挂在某个预印本仓储上（arXiv 或机构库）。

    arXiv 的 source id 是实测得到的常量；机构库用 type == "repository" 兜住。
    """
    for loc in work.locations:
        source = loc.get("source") or {}
        if str(source.get("id") or "") == ARXIV_SOURCE_ID:
            return True
        if (source.get("type") or "") == "repository":
            return True
    return False


def _counts_by_year(raw: Any) -> dict[int, int]:
    """{'year': n} 归一成 {year: n}，并丢掉非数字的脏值。"""
    out: dict[int, int] = {}
    for row in raw or []:
        try:
            out[int(row["year"])] = int(row["cited_by_count"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


_JUNK_TITLES = {"-", "--", "---", "n/a", "na", "untitled", "none", "null"}


def _is_plausible_title(title: str) -> bool:
    """挡掉 OpenAlex 里偶发的占位标题。

    实测见过 title 为 "---" 的条目——它会被当成一篇论文进入候选池，
    然后在报告里以「---」的标题出现。这类条目在源头丢掉最省事。
    """
    if not title or len(title) < 4:
        return False
    stripped = title.strip().strip("-–—_. ").strip()
    if not stripped or stripped.lower() in _JUNK_TITLES:
        return False
    # 至少要有一个字母或 CJK 字符
    return any(ch.isalpha() or "一" <= ch <= "鿿" for ch in stripped)


def _cites_per_year(citations: int, published: datetime | None) -> float:
    """速度代理：篇均年引用数。

    ★ 分母下限 0.5 年是刻意的：一篇三天前上线的论文若已有 5 次引用，
      按真实年龄算会得到 ~600 次/年这种荒谬的数字，把排序整个带偏。
      半个月的「最短观察窗」让新论文的快引用仍然突出，但不至于炸掉。
    """
    if published is None:
        return 0.0
    years = max(0.5, (datetime.now(published.tzinfo) - published).total_seconds() / 31_557_600.0)
    return citations / years


def _now() -> float:
    import time

    return time.monotonic()
