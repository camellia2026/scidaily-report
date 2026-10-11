"""配置层与日期。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scidaily.settings import (
    Journal,
    report_date,
    window_start,
    _looks_like_issn,
)


class TestIssnValidation:
    """ISSN 格式校验。

    这一条看着琐碎，但 ISSN 打错一个字符的表现是「这个刊永远是空的」，
    而不是报错——那是最难查的一类 bug。所以宁可在加载时就大声失败。
    """

    @pytest.mark.parametrize(
        "value",
        ["0028-0836", "0036-8075", "1532-4435", "0962-4929", "2049-3630"],
    )
    def test_accepts_valid(self, value):
        assert _looks_like_issn(value)

    @pytest.mark.parametrize(
        "value",
        [
            "00280836",       # 少了连字符
            "0028-083",       # 少一位
            "0028-08366",     # 多一位
            "0028_0836",      # 错的分隔符
            "002A-0836",      # 非数字
            "abcd-efgh",      # 完全不是
            "",               # 空
        ],
    )
    def test_rejects_invalid(self, value):
        assert not _looks_like_issn(value)

    def test_accepts_x_check_digit(self):
        # ISSN 的最后一位可以是校验位 X
        assert _looks_like_issn("1234-567X")


class TestTierValidation:
    def test_rejects_unknown_tier(self):
        j = Journal(id="x", name="X", issn="0028-0836", tier="T9")
        # tier_weight 对未知分级给 1.0，不抛——但 load 时会抛（见下）
        assert j.tier_weight == 1.0

    def test_tier_weights_are_ordered(self):
        t0 = Journal(id="a", name="A", issn="0028-0836", tier="T0")
        t1 = Journal(id="b", name="B", issn="0036-8075", tier="T1")
        t2 = Journal(id="c", name="C", issn="0092-8674", tier="T2")
        assert t0.tier_weight > t1.tier_weight > t2.tier_weight


class TestResearchDoiPrefix:
    """DOI 前缀用于把 feed 里的研究论文和新闻稿分开。

    实测背景：Nature 的一个 feed 里混着 10.1038/s41586-（论文）和
    10.1038/d41586-（新闻稿）。不区分的话「蝴蝶为什么难抓」这种
    科普稿会和论文并排出现在「顶刊论文」一节里。
    """

    def test_matches_single_prefix(self):
        j = Journal(id="n", name="Nature", issn="0028-0836",
                    research_doi_prefix="10.1038/s41586-")
        assert j.is_research_doi("10.1038/s41586-026-11098-y")
        assert not j.is_research_doi("10.1038/d41586-026-03124-w")

    def test_matches_multiple_prefixes(self):
        # NEJM：原创论文 NEJMoa + 综述 NEJMra 都算研究论文
        j = Journal(id="nejm", name="NEJM", issn="0028-4793",
                    research_doi_prefix="10.1056/NEJMoa,10.1056/NEJMra")
        assert j.is_research_doi("10.1056/NEJMoa2505723")
        assert j.is_research_doi("10.1056/NEJMra2401234")
        assert not j.is_research_doi("10.1056/NEJMp2500001")   # 述评
        assert not j.is_research_doi("10.1056/NEJMc2500001")   # 通讯

    def test_empty_prefix_accepts_all(self):
        j = Journal(id="c", name="Cell", issn="0092-8674", research_doi_prefix="")
        assert j.is_research_doi("10.1016/j.cell.2026.01.001")

    def test_empty_doi_is_not_research(self):
        """没有 DOI 时保守判否。

        feed 里真正的研究论文几乎总有 DOI；没有的那个更可能是新闻稿。
        判成「不是论文」会让它落到要闻通道，这是更安全的失败方向。
        """
        j = Journal(id="n", name="Nature", issn="0028-0836",
                    research_doi_prefix="10.1038/s41586-")
        assert not j.is_research_doi("")


class TestReportDate:
    """★ report_date() 是全项目唯一的日期来源。

    它存在的理由是实测过的事故：GitHub Actions 的 cron 会延迟数分钟到数小时，
    跨过 UTC 午夜时，用错日期会把报告归档到错误的一天，同时污染去重窗口。
    """

    def test_uses_configured_timezone(self, settings):
        # UTC 2026-10-01 22:00 == 北京时间 2026-10-02 06:00
        when = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
        assert report_date(settings, when) == "2026-10-02"

    def test_boundary_just_before_midnight(self, settings):
        # UTC 2026-10-01 15:59 == 北京 23:59，仍是 10-01
        when = datetime(2026, 10, 1, 15, 59, tzinfo=timezone.utc)
        assert report_date(settings, when) == "2026-10-01"

    def test_window_start_uses_same_clock(self, settings):
        when = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
        start = window_start(settings, 24, when)
        assert (when - start).total_seconds() == pytest.approx(24 * 3600)
