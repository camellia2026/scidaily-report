"""预印本 <-> 正式发表版的合并，以及「版本升级」判定（增强点 3）。

**合并为什么几乎免费**：OpenAlex 已经把同一篇论文的各个版本聚在同一个 work
下（实测：一篇 Nature 论文的 locations 里同时有 Nature/publishedVersion、
PubMed、PubMed Central、机构库/submittedVersion）。所以合并的实质不是
「事后拼」，而是**从一开始就别把它们拆开**——按 work id 去重即可。

**真正需要动脑的是「版本升级」**：
一篇上周以 arXiv 预印本身份报过的论文，本周正式发表在 Nature 上。
按「不重复上报」的铁律它该被静默——但那本身就是一条新闻，
而且是科研人员最想知道的那类新闻。

所以规则从「不重复上报」升级成「**不重复上报，但升级要报一次**」：
seen 里每条记下上次上报时的版本类型，升级时放行一次并改写记录。
"""

from __future__ import annotations

from ..state import SeenState, normalize_doi, normalize_title
from .base import Paper


def merge_key(paper: Paper) -> str:
    """运行内去重的键。

    优先级 work_id > doi > 标题指纹：
      work_id 是 OpenAlex 的聚类结果，最可靠；
      doi 次之（少数记录没有 work_id）；
      标题指纹最不可靠（不同论文可能同名），所以放最后，且只在前面都缺时用。
    """
    if paper.work_id:
        return "w:" + paper.work_id
    if paper.doi:
        return "d:" + normalize_doi(paper.doi)
    return "t:" + normalize_title(paper.title)


def merge_records(papers: list[Paper]) -> list[Paper]:
    """把同一次运行里指向同一篇论文的记录合并成一条。

    选「主记录」的规则（按优先级）：
      1. venue_type == journal 胜过 preprint —— 正式版信息更全、更权威
      2. tier 高者胜（T0 > T1 > T2）
      3. 摘要更长者胜（信息量更大的那条）
      4. 引用数更高者胜
    并且从落选记录里**补齐**主记录缺失的字段（比如预印本有摘要而正式版没有）。
    """
    groups: dict[str, list[Paper]] = {}
    for p in papers:
        groups.setdefault(merge_key(p), []).append(p)

    merged: list[Paper] = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        group = sorted(group, key=_rank, reverse=True)
        primary = group[0]
        for other in group[1:]:
            _backfill(primary, other)
        merged.append(primary)
    return merged


def _rank(p: Paper) -> tuple:
    return (
        1 if p.venue_type == "journal" else 0,
        {"T0": 3, "T1": 2, "T2": 1}.get(p.tier, 0),
        len(p.abstract or ""),
        p.citation_count,
    )


def _backfill(primary: Paper, other: Paper) -> None:
    """用落选记录补齐主记录的空字段。只填空，不覆盖已有值。"""
    if not primary.abstract and other.abstract:
        primary.abstract = other.abstract
    if not primary.doi and other.doi:
        primary.doi = other.doi
    if not primary.authors and other.authors:
        primary.authors = other.authors
    if not primary.oa_url and other.oa_url:
        primary.oa_url = other.oa_url
    if other.citation_count > primary.citation_count:
        primary.citation_count = other.citation_count
        primary.cites_per_year = other.cites_per_year
    if not primary.locations and other.locations:
        primary.locations = other.locations


def find_venue_upgrades(papers: list[Paper], state: SeenState) -> list[Paper]:
    """找出「上次以预印本上报、现在有了正式期刊版」的论文。

    这是唯一允许绕过「已见即静默」的通道，且只放行一次：
    调用方上报后必须调 state.mark_upgraded()，否则下周会重复报。
    """
    out: list[Paper] = []
    for p in papers:
        if p.venue_type != "journal" or not p.work_id:
            continue
        if state.is_venue_upgrade(p.work_id, p.venue_type):
            out.append(p)
    return out


def resolve_work_ids(papers: list[Paper]) -> int:
    """用 DOI 把「没有 work_id 的记录」对齐到 OpenAlex 的 work。

    为什么需要这一步：feed（rss.py）能给出 DOI 但给不出 work_id，
    而 work_id 才是跨版本（预印本<->正式版）合并的正确键。
    没有它，同一篇 Nature 论文会以「OpenAlex 记录」和「feed 记录」两条身份
    各占一席——去重看起来做了，其实没做。

    做法：先从「同时有 work_id 和 doi」的记录建一张 doi -> work_id 的索引，
    再回填给只有 doi 的记录。

    返回被回填的条数。
    """
    doi_to_work: dict[str, str] = {}
    for p in papers:
        if p.work_id and p.doi:
            key = normalize_doi(p.doi)
            if key:
                doi_to_work.setdefault(key, p.work_id)

    filled = 0
    for p in papers:
        if p.work_id or not p.doi:
            continue
        work = doi_to_work.get(normalize_doi(p.doi))
        if work:
            p.work_id = work
            filled += 1
    return filled


def dedupe_in_run(papers: list[Paper]) -> tuple[list[Paper], int]:
    """返回 (去重后的列表, 丢弃条数)。先对齐 work_id，再合并。"""
    resolve_work_ids(papers)
    merged = merge_records(papers)
    return merged, max(0, len(papers) - len(merged))
