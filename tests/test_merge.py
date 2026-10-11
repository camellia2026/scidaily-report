"""预印本 <-> 正式发表版的合并（增强点 3），以及 OpenAlex 数据的映射。

这里的每一条都对应一个真实观察到的失败模式，不是凭空想的边界。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scidaily.sources.base import decode_abstract_inverted_index
from scidaily.sources.merge import (
    dedupe_in_run,
    find_venue_upgrades,
    merge_key,
    merge_records,
    resolve_work_ids,
)
from scidaily.sources.openalex import (
    PREPRINT_TYPES,
    _Work,
    _is_plausible_title,
    _looks_like_non_research,
    _resolve_venue,
    _cites_per_year,
    _counts_by_year,
)
from scidaily.settings import Journal
from scidaily.state import SeenState

from conftest import make_paper


class TestMergeKey:
    def test_prefers_work_id(self):
        p = make_paper(work_id="W1", doi="10.1/a", title="T")
        assert merge_key(p) == "w:W1"

    def test_falls_back_to_doi(self):
        p = make_paper(work_id="", doi="10.1/a", title="T")
        assert merge_key(p).startswith("d:")

    def test_falls_back_to_title(self):
        p = make_paper(work_id="", doi="", title="Some Title")
        assert merge_key(p).startswith("t:")


class TestResolveWorkIds:
    """让 feed 来的记录对齐到 OpenAlex 的 work。

    没有这一步，同一篇 Nature 论文会以「OpenAlex 记录」和「feed 记录」
    两条身份各占一席——去重看起来做了，其实没做。
    """

    def test_backfills_work_id_from_doi(self):
        authoritative = make_paper(work_id="W42", doi="10.1038/x")
        feed_record = make_paper(work_id="", doi="10.1038/X")   # 大小写不同
        filled = resolve_work_ids([authoritative, feed_record])
        assert filled == 1
        assert feed_record.work_id == "W42"

    def test_no_change_when_already_has_work_id(self):
        a = make_paper(work_id="W1", doi="10.1/a")
        b = make_paper(work_id="W2", doi="10.1/a")
        assert resolve_work_ids([a, b]) == 0

    def test_records_without_doi_are_untouched(self):
        p = make_paper(work_id="", doi="", title="Solo")
        assert resolve_work_ids([p]) == 0
        assert p.work_id == ""


class TestMergeRecords:
    def test_journal_version_wins_over_preprint(self):
        pre = make_paper(work_id="W1", venue_type="preprint", abstract="x" * 10)
        pub = make_paper(work_id="W1", venue_type="journal", abstract="")
        merged = merge_records([pre, pub])
        assert len(merged) == 1
        assert merged[0].venue_type == "journal"

    def test_backfills_missing_abstract_from_loser(self):
        """正式版常常没有摘要，而预印本有。合并要顺手补齐。"""
        pre = make_paper(work_id="W1", venue_type="preprint", abstract="预印本摘要")
        pub = make_paper(work_id="W1", venue_type="journal", abstract="")
        merged = merge_records([pre, pub])
        assert merged[0].abstract == "预印本摘要"

    def test_does_not_overwrite_existing_abstract(self):
        pre = make_paper(work_id="W1", venue_type="preprint", abstract="旧")
        pub = make_paper(work_id="W1", venue_type="journal", abstract="新")
        merged = merge_records([pre, pub])
        assert merged[0].abstract == "新"

    def test_keeps_higher_citation_count(self):
        a = make_paper(work_id="W1", citations=5)
        b = make_paper(work_id="W1", citations=50)
        merged = merge_records([a, b])
        assert merged[0].citation_count == 50

    def test_dedupe_in_run_counts_merges(self):
        papers = [make_paper(work_id="W1"), make_paper(work_id="W1"), make_paper(work_id="W2")]
        merged, dropped = dedupe_in_run(papers)
        assert len(merged) == 2
        assert dropped == 1

    def test_distinct_papers_survive(self):
        papers = [make_paper(work_id=f"W{i}", title=f"T{i}") for i in range(5)]
        merged, dropped = dedupe_in_run(papers)
        assert len(merged) == 5 and dropped == 0


class TestFindVenueUpgrades:
    def test_detects_preprint_that_became_journal(self):
        state = SeenState()
        state.mark("2026-10-01", work_id="W1", venue="preprint")
        paper = make_paper(work_id="W1", venue_type="journal")
        assert find_venue_upgrades([paper], state) == [paper]

    def test_ignores_brand_new_papers(self):
        state = SeenState()
        paper = make_paper(work_id="W-new", venue_type="journal")
        assert find_venue_upgrades([paper], state) == []

    def test_ignores_still_preprint(self):
        state = SeenState()
        state.mark("2026-10-01", work_id="W1", venue="preprint")
        paper = make_paper(work_id="W1", venue_type="preprint")
        assert find_venue_upgrades([paper], state) == []


class TestAbstractReconstruction:
    """OpenAlex 把摘要存成 {词: [出现位置]}，必须还原。"""

    def test_basic_order(self):
        got = decode_abstract_inverted_index({"Deep": [0], "learning": [1], "works": [2]})
        assert got == "Deep learning works"

    def test_out_of_order_keys(self):
        got = decode_abstract_inverted_index({"works": [2], "Deep": [0], "learning": [1]})
        assert got == "Deep learning works"

    def test_word_appearing_twice(self):
        got = decode_abstract_inverted_index({"a": [0, 2], "b": [1]})
        assert got == "a b a"

    @pytest.mark.parametrize("bad", [None, {}, {"w": None}, {"w": "x"}, []])
    def test_malformed_input_returns_empty_not_crash(self, bad):
        """社论/通讯常常没有摘要。这里绝不能抛——它只是一条没有摘要的论文。"""
        assert decode_abstract_inverted_index(bad) == ""

    def test_gaps_do_not_crash(self):
        """位置序列可能不连续（OpenAlex 偶尔缺词），不能假定 len == 词数。"""
        got = decode_abstract_inverted_index({"a": [0], "b": [7]})
        assert "a" in got and "b" in got

    def test_truncates_on_word_boundary(self):
        index = {f"w{i}": [i] for i in range(600)}
        got = decode_abstract_inverted_index(index, max_chars=50)
        assert len(got) <= 52
        assert got.endswith("…")


class TestVenueResolution:
    """★ 这里钉住一个实测踩到的坑：

    OpenAlex 的 `issn_l` 是 linking ISSN，**不等于**通常引用的印刷版 ISSN。
    实测：JAMA 的 issn_l 是 0002-9955（印刷版 0098-7484），
          The Lancet 的 issn_l 是 0099-5355（印刷版 0140-6736）。

    只比 issn_l 的话，这两个 T0 医学刊的论文会被判成「未追踪期刊」，
    静默降级成 T2/综合——分级权重和学科分区同时失效，且不报错。
    """

    def _work(self, locations):
        return _Work(
            {
                "id": "https://openalex.org/W1",
                "title": "T",
                "locations": locations,
                "primary_location": locations[0] if locations else {},
            }
        )

    def test_matches_via_issn_array_when_issn_l_differs(self):
        jama = Journal(id="jama", name="JAMA", issn="0098-7484", tier="T0", discipline="医学")
        work = self._work(
            [
                {
                    "source": {
                        "display_name": "JAMA",
                        "issn_l": "0002-9955",              # ← linking ISSN
                        "issn": ["0098-7484", "1533-4406"],  # ← 含印刷版
                        "type": "journal",
                    }
                }
            ]
        )
        journal, issn, venue = _resolve_venue(work, {"0098-7484": jama})
        assert journal is jama
        assert venue == "journal"

    def test_arxiv_only_is_preprint(self):
        work = self._work(
            [{"source": {"id": "https://openalex.org/S4306400194", "type": "repository"}}]
        )
        journal, _, venue = _resolve_venue(work, {})
        assert journal is None and venue == "preprint"

    def test_untracked_journal_is_still_journal(self):
        """不在清单里但已正式发表 —— 它的预印本身份已经结束了。"""
        work = self._work(
            [{"source": {"display_name": "PLoS ONE", "issn_l": "1932-6203", "type": "journal"}}]
        )
        journal, _, venue = _resolve_venue(work, {})
        assert journal is None and venue == "journal"


class TestNonResearchDetection:
    """识别「根本不是研究内容」的条目。

    背景：JAMA / NEJM 在 OpenAlex 里发的新闻稿、人文小品、播客，
    type 全是 `article`，`type_crossref` 又是空的——OpenAlex 没有任何
    可以直接分辨它们的信息。实测漏到报告里的有：
        "Holding Hope"（人文小品）、"The Mayor"、
        "JAMA"（刊级记录）、"Editors' Summary Podcast"
    """

    def _work(self, **raw):
        base = {"id": "https://openalex.org/W1", "title": "T", "locations": [], "primary_location": {}}
        base.update(raw)
        return _Work(base)

    def test_all_three_signals_absent_is_non_research(self):
        assert _looks_like_non_research(self._work())

    def test_abstract_present_is_research(self):
        w = self._work(abstract_inverted_index={"a": [0]})
        assert not _looks_like_non_research(w)

    def test_topic_present_is_research(self):
        w = self._work(primary_topic={"display_name": "Oncology"})
        assert not _looks_like_non_research(w)

    def test_references_present_is_research(self):
        """★ 这一条最关键：摘要还没生成的新论文，靠参考文献保住。"""
        w = self._work(referenced_works_count=42)
        assert not _looks_like_non_research(w)


class TestTitleSanity:
    """实测见过 title 为 "---" 的条目，它会被当成论文进报告。"""

    @pytest.mark.parametrize("bad", ["", "-", "--", "---", "n/a", "   ", "..."])
    def test_rejects_junk(self, bad):
        assert not _is_plausible_title(bad)

    @pytest.mark.parametrize(
        "good",
        ["Deep learning", "深度学习方法", "Cancer", "On the Origin of Species"],
    )
    def test_accepts_real_titles(self, good):
        assert _is_plausible_title(good)

    def test_single_letter_is_rejected(self):
        """4 字符下限是有意的：真实论文标题极少短于此，
        而 OpenAlex 里的占位垃圾往往正是极短串。"""
        assert not _is_plausible_title("A")


class TestPreprintTypes:
    """预印本通道用白名单而不是黑名单。

    实测漏进来过 "ANU Press eBooks" 的 book-chapter：
    它同时挂着 arXiv 与 RePEc 两个 repository location，
    「有没有仓储 location」这道关拦不住它。
    """

    def test_book_chapter_excluded(self):
        assert "book-chapter" not in PREPRINT_TYPES

    def test_preprint_and_article_allowed(self):
        assert "preprint" in PREPRINT_TYPES
        assert "article" in PREPRINT_TYPES


class TestCitationHelpers:
    def test_counts_by_year_normalizes(self):
        raw = [{"year": 2026, "cited_by_count": 5}, {"year": 2025, "cited_by_count": 3}]
        assert _counts_by_year(raw) == {2026: 5, 2025: 3}

    def test_counts_by_year_skips_garbage(self):
        raw = [{"year": "x", "cited_by_count": 5}, {"year": 2026}, {"year": 2025, "cited_by_count": 3}]
        assert _counts_by_year(raw) == {2025: 3}

    def test_cites_per_year_has_floor_denominator(self):
        """★ 分母下限 0.5 年是刻意的。

        一篇三天前上线的论文若有 5 次引用，按真实年龄算会得到 ~600 次/年
        这种荒谬数字，把排序整个带偏。
        """
        published = datetime.now(timezone.utc)
        value = _cites_per_year(5, published)
        assert value <= 10.0     # 不是 600

    def test_cites_per_year_normal_case(self):
        published = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=365 * 2)
        assert _cites_per_year(20, published) == pytest.approx(10.0, rel=0.05)

    def test_cites_per_year_no_date(self):
        assert _cites_per_year(100, None) == 0.0
