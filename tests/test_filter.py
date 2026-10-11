"""过滤层：去重、窗口、预打分，以及候选编号。

★ TestRefNumbering 钉住的是一个真实踩到的 bug：
  五个通道各自从 1 开始编号，直接拼起来就会有五个 ref=1。
  而 ref 是模型与数据的唯一契约——错位的 ref 查不出「越界」，
  只会让条目静默地落到错误的节里，比报错更难发现。
"""

from __future__ import annotations

from datetime import datetime, timezone

from scidaily.filter import (
    Candidate,
    build_candidates,
    mark_seen,
    pre_score,
)
from scidaily.state import SeenState

from conftest import make_news, make_paper

NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


class TestFreshness:
    def test_recent_scores_higher_than_old(self, settings):
        fresh = make_paper(work_id="W1", hours_ago=1)
        old = make_paper(work_id="W2", hours_ago=100)
        assert pre_score(fresh, settings, NOW)[0] > pre_score(old, settings, NOW)[0]

    def test_papers_use_longer_half_life_than_news(self, settings):
        """★ 论文老化得慢：三天前的 Nature 论文仍值得看，三天前的新闻不一定。

        配置里论文半衰期 48h，原项目新闻用 12h。
        """
        paper = make_paper(work_id="W1", hours_ago=24)
        news = make_news(hours_ago=24)
        _, paper_reasons = pre_score(paper, settings, NOW)
        _, news_reasons = pre_score(news, settings, NOW)

        paper_fresh = float([r for r in paper_reasons if r.startswith("新鲜")][0][2:])
        news_fresh = float([r for r in news_reasons if r.startswith("新鲜")][0][2:])
        assert paper_fresh > news_fresh

    def test_undated_gets_neutral_score(self, settings):
        """没日期 -> 不奖不罚取中间值。

        给 1.0 会让无日期条目永远排最前；给 0 等于丢弃。
        """
        undated = make_paper(work_id="W1", hours_ago=None)
        _, reasons = pre_score(undated, settings, NOW)
        fresh = float([r for r in reasons if r.startswith("新鲜")][0][2:])
        assert fresh == 0.5


class TestTierWeight:
    def test_t0_beats_t1_beats_t2(self, settings):
        t0 = make_paper(work_id="W1", tier="T0", weight=3.0)
        t1 = make_paper(work_id="W2", tier="T1", weight=2.0)
        t2 = make_paper(work_id="W3", tier="T2", weight=1.4)
        s0 = pre_score(t0, settings, NOW)[0]
        s1 = pre_score(t1, settings, NOW)[0]
        s2 = pre_score(t2, settings, NOW)[0]
        assert s0 > s1 > s2


class TestVelocityFactor:
    def test_highly_cited_paper_gets_boost(self, settings):
        cited = make_paper(work_id="W1", hours_ago=48, cites_per_year=100.0)
        plain = make_paper(work_id="W2", hours_ago=48, cites_per_year=0.0)
        assert pre_score(cited, settings, NOW)[0] > pre_score(plain, settings, NOW)[0]

    def test_boost_is_bounded(self, settings):
        """对数压缩 + 上限，否则一篇万引论文会把排序整个压平。"""
        absurd = make_paper(work_id="W1", hours_ago=48, cites_per_year=1_000_000.0)
        _, reasons = pre_score(absurd, settings, NOW)
        boost = float([r for r in reasons if r.startswith("速度")][0].split("x")[1])
        assert boost <= 2.5


class TestKeywordFactor:
    def test_positive_keyword_boosts(self, settings):
        hit = make_paper(work_id="W1", title="A breakthrough in protein folding")
        plain = make_paper(work_id="W2", title="A routine measurement")
        assert pre_score(hit, settings, NOW)[0] > pre_score(plain, settings, NOW)[0]

    def test_penalty_keyword_lowers(self, settings):
        bad = make_paper(work_id="W1", title="Author correction to previous work")
        plain = make_paper(work_id="W2", title="A routine measurement")
        assert pre_score(bad, settings, NOW)[0] < pre_score(plain, settings, NOW)[0]


class TestBuildCandidates:
    def test_window_filters_old_items(self, settings):
        items = [
            make_paper(work_id="W1", hours_ago=1),
            make_paper(work_id="W2", hours_ago=500),
        ]
        cands, stats = build_candidates(
            items, SeenState(), settings, NOW, channel="paper", max_age_hours=72
        )
        assert len(cands) == 1
        assert stats.dropped_by_window == 1

    def test_undated_items_bypass_window(self, settings):
        items = [make_paper(work_id="W1", hours_ago=None)]
        cands, _ = build_candidates(
            items, SeenState(), settings, NOW, channel="paper", max_age_hours=72
        )
        assert len(cands) == 1

    def test_seen_items_are_dropped(self, settings):
        state = SeenState()
        state.mark("2026-10-01", work_id="W1")
        items = [make_paper(work_id="W1"), make_paper(work_id="W2")]
        cands, stats = build_candidates(items, state, settings, NOW, channel="paper")
        assert len(cands) == 1
        assert stats.already_seen == 1

    def test_ignore_seen_bypasses(self, settings):
        state = SeenState()
        state.mark("2026-10-01", work_id="W1")
        items = [make_paper(work_id="W1")]
        cands, _ = build_candidates(
            items, state, settings, NOW, channel="paper", ignore_seen=True
        )
        assert len(cands) == 1

    def test_in_run_duplicates_dropped(self, settings):
        items = [make_paper(work_id="W1"), make_paper(work_id="W1")]
        cands, stats = build_candidates(items, SeenState(), settings, NOW, channel="paper")
        assert len(cands) == 1
        assert stats.dup_in_run == 1

    def test_penalty_only_items_dropped(self, settings):
        items = [make_paper(work_id="W1", title="Erratum: something")]
        cands, stats = build_candidates(items, SeenState(), settings, NOW, channel="paper")
        assert len(cands) == 0
        assert stats.dropped_by_penalty == 1

    def test_cap_is_respected(self, settings):
        items = [make_paper(work_id=f"W{i}", title=f"T{i}") for i in range(300)]
        cands, _ = build_candidates(items, SeenState(), settings, NOW, channel="paper")
        assert len(cands) == settings.channel_cap("paper")


class TestRefNumbering:
    """★ ref 必须在**截断之后**分配，且跨通道唯一。"""

    def test_refs_are_contiguous_after_truncation(self, settings):
        items = [make_paper(work_id=f"W{i}", title=f"T{i}") for i in range(300)]
        cands, _ = build_candidates(items, SeenState(), settings, NOW, channel="paper")
        refs = [c.ref for c in cands]
        assert refs == list(range(1, len(cands) + 1))

    def test_refs_unique_within_channel(self, settings):
        items = [make_paper(work_id=f"W{i}", title=f"T{i}") for i in range(50)]
        cands, _ = build_candidates(items, SeenState(), settings, NOW, channel="paper")
        assert len({c.ref for c in cands}) == len(cands)

    def test_sorted_by_score_descending(self, settings):
        items = [
            make_paper(work_id="W1", title="A breakthrough", tier="T0", weight=3.0),
            make_paper(work_id="W2", title="Routine", tier="T2", weight=1.4),
        ]
        cands, _ = build_candidates(items, SeenState(), settings, NOW, channel="paper")
        assert cands[0].score >= cands[1].score


class TestMarkSeen:
    def test_marks_all_candidates_not_just_published(self):
        """★ 标记**全部**候选，不只是发布的那几条。

        原项目的教训：只标记发布的，落选的那几十条明天会重新进候选池，
        天天占位却不产出——候选池会被自己的落选者堵死。
        """
        state = SeenState()
        cands = [
            Candidate(
                ref=i, channel="paper",
                # 标题必须各不相同：标题指纹也是去重键之一，
                # 五篇同名论文会被判成同一篇（这是正确行为，不是 bug）。
                record=make_paper(work_id=f"W{i}", title=f"Distinct Study {i}"),
                score=1.0,
            )
            for i in range(1, 6)
        ]
        marked = mark_seen([c.record for c in cands], state, "2026-10-02")
        assert marked == 5
        assert state.is_seen(work_id="W5")

    def test_records_venue_for_upgrade_tracking(self):
        state = SeenState()
        paper = make_paper(work_id="W1", venue_type="preprint")
        mark_seen([paper], state, "2026-10-02")
        assert state.is_venue_upgrade("W1", "journal")


class TestPromptLine:
    def test_includes_essential_metadata(self):
        cand = Candidate(
            ref=3, channel="paper",
            record=make_paper(title="A Study", journal="Nature", tier="T0",
                              discipline="综合", citations=42),
            score=1.0,
        )
        line = cand.to_prompt_line()
        assert "[3]" in line
        assert "Nature" in line and "T0" in line and "42" in line

    def test_marks_preprints_explicitly(self):
        """预印本身份必须在 prompt 里标出来，否则模型不会加「未经同行评审」。"""
        cand = Candidate(
            ref=1, channel="preprint",
            record=make_paper(venue_type="preprint"),
            score=1.0,
        )
        assert "预印本" in cand.to_prompt_line()
