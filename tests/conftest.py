"""pytest 共享夹具。

★ 全部离线：不联网、不调模型。所有需要网络的地方都用合成的 fixture 数据。
  这样测试才是确定性的——一个「有时候通有时候不通」的测试套件
  会很快被忽略，那比没有测试更糟。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scidaily.settings import Journal, Settings  # noqa: E402
from scidaily.sources.base import NewsItem, Paper  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """一份不读 config/ 的 Settings。测试不该依赖仓库里的 YAML。"""
    journals = [
        Journal(id="nature", name="Nature", issn="0028-0836", tier="T0",
                discipline="综合", route=["openalex", "rss"],
                feed="https://www.nature.com/nature.rss",
                research_doi_prefix="10.1038/s41586-"),
        Journal(id="jama", name="JAMA", issn="0098-7484", tier="T0",
                discipline="医学", route=["pubmed", "openalex"]),
        Journal(id="tpami", name="IEEE TPAMI", issn="0162-8828", tier="T1",
                discipline="计算机", route=["openalex"]),
        Journal(id="jmlr", name="JMLR", issn="1532-4435", tier="T1",
                discipline="计算机", route=["openalex"]),
    ]
    return Settings(
        journals=journals,
        sources=[],
        windows={"news": 24, "paper": 72, "weekly_days": 14, "weekly_fallback_days": 90},
        report={"max_title_chars": 90, "max_summary_chars": 200},
        channels={
            "news": {"max_candidates": 30, "quota": 5},
            "paper": {"max_candidates": 50, "quota": 8},
            "hot": {"max_candidates": 12, "quota": 3},
            "preprint": {"max_candidates": 15, "quota": 4},
            "upgrade": {"max_candidates": 10, "quota": 5},
        },
        prefilter={
            "max_candidates": 80,
            "freshness_half_life_h": 48,
            "keyword_boost": 2.0,
            "keywords": ["breakthrough", "first", "clinical trial"],
            "keyword_penalty": 0.4,
            "penalty_keywords": ["erratum", "correction", "in this issue"],
        },
        velocity={"max_age_months": 24, "recent_window_days": 90, "shortlist": 10},
        preprints={"window_days": 60, "min_citations": 5, "limit": 60},
        state={"prune_days": 90},
        notify={"channel": "console", "subject_prefix": "[科学前沿]", "send_on_empty": False},
        api={"mailto": "test@example.com"},
        timezone="Asia/Shanghai",
    )


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


def make_paper(
    title: str = "A Study",
    *,
    work_id: str = "https://openalex.org/W1",
    doi: str | None = None,
    journal: str = "Nature",
    issn: str = "0028-0836",
    tier: str = "T0",
    discipline: str = "综合",
    hours_ago: float | None = 5,
    citations: int = 0,
    cites_per_year: float = 0.0,
    counts_by_year: dict | None = None,
    venue_type: str = "journal",
    abstract: str = "",
    weight: float = 3.0,
) -> Paper:
    published = (
        datetime.now(timezone.utc) - timedelta(hours=hours_ago) if hours_ago is not None else None
    )
    # ★ DOI 由 work_id 派生，保证不同 work 拿到不同 DOI。
    #   让所有夹具共用一个默认 DOI 会伪造出「不同论文共享 DOI」的假象，
    #   把去重测试变成一场误会——实测踩到过。
    if doi is None:
        suffix = work_id.rsplit("/", 1)[-1].lower() or "x"
        doi = f"10.1038/test-{suffix}"
    return Paper(
        work_id=work_id,
        title=title,
        url=f"https://doi.org/{doi}",
        journal=journal,
        issn=issn,
        tier=tier,
        discipline=discipline,
        doi=doi,
        authors=["A. Author"],
        published=published,
        citation_count=citations,
        cites_per_year=cites_per_year,
        counts_by_year=counts_by_year or {},
        abstract=abstract,
        venue_type=venue_type,
        source_id="test",
        source_name="test",
        weight=weight,
    )


def make_news(
    title: str = "A Report",
    *,
    hours_ago: float | None = 3,
    url: str = "https://example.org/a",
    weight: float = 1.5,
) -> NewsItem:
    published = (
        datetime.now(timezone.utc) - timedelta(hours=hours_ago) if hours_ago is not None else None
    )
    return NewsItem(
        title=title,
        url=url,
        summary="",
        source_id="test",
        source_name="Test Wire",
        published=published,
        weight=weight,
    )
