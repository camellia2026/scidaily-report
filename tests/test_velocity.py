"""引用速度指标（增强点 4）。

核心是把「热文」和「名篇」分开。这里钉住的是指标的**区分能力**，
而不只是公式算得对——公式算得对但没有区分度是没用的。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scidaily.velocity import HotScore, compute_hotness, rank_hot

from conftest import make_paper

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


def _paper(months_ago: float, total: int, recent: int, cpy: float = 0.0):
    published = NOW - timedelta(days=months_ago * 30.44)
    p = make_paper(work_id=f"W-{months_ago}-{total}", citations=total)
    p.published = published
    p.counts_by_year = {2026: recent, 2025: 0}
    p.cites_per_year = cpy
    return p


class TestHotnessMetric:
    def test_recent_high_ratio_beats_old_high_total(self):
        """这是整个指标存在的理由。

        老名篇：总引用极高，但引用摊在历史上 -> 近年占比低
        真热文：总引用不高，但引用几乎全在近期 -> 近年占比高
        """
        old_famous = _paper(months_ago=20, total=1000, recent=30)
        new_hot = _paper(months_ago=6, total=80, recent=78)

        old_score = compute_hotness(old_famous, NOW)
        new_score = compute_hotness(new_hot, NOW)

        assert new_score.hotness > old_score.hotness

    def test_ratio_and_volume_both_matter(self):
        """只有占比、没有量 -> 被引 2 次的新论文会压过一切。"""
        tiny = _paper(months_ago=1, total=2, recent=2)          # 占比 1.0，量极小
        substantial = _paper(months_ago=6, total=200, recent=180)  # 占比 0.9，量大
        assert compute_hotness(substantial, NOW).hotness > compute_hotness(tiny, NOW).hotness

    def test_no_citations_returns_none(self):
        assert compute_hotness(_paper(6, total=0, recent=0), NOW) is None

    def test_no_recent_citations_returns_none(self):
        assert compute_hotness(_paper(6, total=500, recent=0), NOW) is None

    def test_no_date_returns_none(self):
        p = make_paper(work_id="W", citations=10)
        p.published = None
        p.counts_by_year = {2026: 10}
        assert compute_hotness(p, NOW) is None

    def test_recent_window_spans_two_calendar_years(self):
        """★ 为什么取两年：

        counts_by_year 只有年度粒度。年初跑的时候当前年只有几周数据，
        单独用它会把所有论文都算成 0。取两年窗口在「够新」和「有信号」之间平衡。
        """
        p = make_paper(work_id="W", citations=100)
        p.published = NOW - timedelta(days=300)
        p.counts_by_year = {2026: 5, 2025: 40, 2024: 55}
        score = compute_hotness(p, NOW)
        assert score.recent == 45      # 2026 + 2025，不含 2024


class TestAgeCap:
    def test_old_papers_are_excluded(self, settings):
        """年龄上限才是「热」与「著名」的分界线。"""
        old = _paper(months_ago=30, total=5000, recent=900)
        new = _paper(months_ago=6, total=100, recent=90)
        ranked = rank_hot([old, new], settings, now=NOW)
        ids = [p.work_id for p, _ in ranked]
        assert old.work_id not in ids
        assert new.work_id in ids

    def test_boundary_just_inside(self, settings):
        inside = _paper(months_ago=23, total=100, recent=90)
        ranked = rank_hot([inside], settings, now=NOW)
        assert len(ranked) == 1


class TestRanking:
    def test_sorted_descending(self, settings):
        papers = [
            _paper(months_ago=6, total=50, recent=20),
            _paper(months_ago=6, total=500, recent=450),
            _paper(months_ago=6, total=100, recent=60),
        ]
        ranked = rank_hot(papers, settings, now=NOW)
        scores = [s.hotness for _, s in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_empty_input(self, settings):
        assert rank_hot([], settings, now=NOW) == []

    def test_explain_is_human_readable(self):
        s = HotScore(recent=90, ratio=0.9, hotness=81.0, age_months=6.0)
        text = s.explain()
        assert "90" in text and "%" in text
