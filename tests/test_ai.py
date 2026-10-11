"""LLM 层：结构化模型的约束、截断与 pydantic 字段上限的一致性。

★ TestClipRespectsSchema 钉住的是实测踩到的崩溃：

  config 里 report.max_title_chars=90（英文标题需要更长），
  而 DigestItem.headline_zh 的 max_length=60。
  规则兜底拿 90 去截中文标题，产出 60+ 字符，
  直接崩在 pydantic 校验上。

  这与原项目 `_clip(text, 60)` 没给省略号留位是同一类错误：
  **截断长度和字段约束必须是同一个事实。**
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scidaily.ai import (
    _LIMIT_HEADLINE_ZH,
    _LIMIT_SUMMARY_ZH,
    _LIMIT_TITLE_EN,
    Digest,
    DigestItem,
    _clip,
    rule_based_digest,
)
from scidaily.filter import Candidate

from conftest import make_news, make_paper


class TestClip:
    def test_short_text_unchanged(self):
        assert _clip("short", 60) == "short"

    @pytest.mark.parametrize("limit", [1, 2, 5, 10, 20, 60, 90, 200])
    def test_never_exceeds_limit(self, limit):
        """★ 这条覆盖了 off-by-one。

        省略号也算一个字符。原项目 _clip(text, 60) 返回了 61 个字符。
        """
        result = _clip("x" * 500, limit)
        assert len(result) <= limit, f"limit={limit} 产出 {len(result)} 字符"

    @pytest.mark.parametrize("limit", [5, 20, 60])
    def test_cjk_never_exceeds_limit(self, limit):
        result = _clip("深" * 500, limit)
        assert len(result) <= limit

    def test_strips_whitespace(self):
        assert _clip("  padded  ", 50) == "padded"

    def test_handles_none_like_input(self):
        assert _clip("", 10) == ""


class TestClipRespectsSchema:
    @pytest.mark.parametrize(
        "config_title_chars",
        [40, 60, 90, 120, 300, 1000],   # 含超过字段上限的值
    )
    def test_rule_mode_never_violates_schema(self, settings, config_title_chars):
        """配置上限高于字段上限时，规则模式必须仍然产出合法对象。"""
        settings.report["max_title_chars"] = config_title_chars
        settings.report["max_summary_chars"] = config_title_chars

        cands = [
            Candidate(
                ref=i, channel="paper",
                record=make_paper(
                    work_id=f"W{i}",
                    title="A Very Long English Title " * 20,
                    abstract="Long abstract text. " * 100,
                ),
                score=1.0,
            )
            for i in range(1, 4)
        ]
        # 不抛 ValidationError 就是通过
        digest = rule_based_digest(cands, settings)
        assert len(digest.items) == 3

    def test_field_limits_are_internally_consistent(self):
        """常量必须与 pydantic 声明一致，否则两处会漂移。"""
        item = DigestItem(
            refs=[1],
            title_en="x" * _LIMIT_TITLE_EN,
            headline_zh="x" * _LIMIT_HEADLINE_ZH,
            summary_zh="x" * _LIMIT_SUMMARY_ZH,
        )
        assert len(item.title_en) == _LIMIT_TITLE_EN

    def test_one_over_the_limit_is_rejected(self):
        """反证：确认上限确实在生效，而不是形同虚设。"""
        with pytest.raises(ValidationError):
            DigestItem(
                refs=[1],
                title_en="x",
                headline_zh="x" * (_LIMIT_HEADLINE_ZH + 1),
                summary_zh="x",
            )


class TestDigestItemValidation:
    def test_blank_headline_rejected(self):
        with pytest.raises(ValidationError):
            DigestItem(refs=[1], title_en="T", headline_zh="   ", summary_zh="ok")

    def test_refs_must_be_non_empty(self):
        with pytest.raises(ValidationError):
            DigestItem(refs=[], title_en="T", headline_zh="标", summary_zh="ok")

    def test_importance_clamped(self):
        with pytest.raises(ValidationError):
            DigestItem(refs=[1], title_en="T", headline_zh="标", summary_zh="ok", importance=11)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("医学", "医学"),
            ("计算机", "计算机"),
            ("计算机科学", "计算机"),
            ("machine learning", "计算机"),
            ("数学", "数学与计算数学"),
            ("nonsense", "综合"),
            ("", "综合"),
        ],
    )
    def test_discipline_aliases_normalized(self, raw, expected):
        item = DigestItem(
            refs=[1], title_en="T", headline_zh="标", summary_zh="ok", discipline=raw
        )
        assert item.discipline == expected


class TestDigestValidation:
    def test_blank_overview_rejected(self):
        with pytest.raises(ValidationError):
            Digest(overview_zh="  ", items=[])

    def test_empty_digest_is_valid(self):
        """一条都没有必须是合法的——「今天没新闻」不是错误。"""
        d = Digest(overview_zh="今日无更新。", items=[], watchlist=[])
        assert d.items == []


class TestRuleBasedDigest:
    def test_produces_one_item_per_candidate(self, settings):
        cands = [
            Candidate(ref=i, channel="paper", record=make_paper(work_id=f"W{i}"), score=1.0)
            for i in range(1, 4)
        ]
        digest = rule_based_digest(cands, settings)
        assert len(digest.items) == 3
        assert [i.refs[0] for i in digest.items] == [1, 2, 3]

    def test_empty_candidates(self, settings):
        digest = rule_based_digest([], settings)
        assert digest.items == []

    def test_missing_abstract_uses_placeholder(self, settings):
        cand = Candidate(
            ref=1, channel="paper",
            record=make_paper(work_id="W1", abstract=""),
            score=1.0,
        )
        digest = rule_based_digest([cand], settings)
        assert digest.items[0].summary_zh.strip()

    def test_news_record_handled(self, settings):
        """规则兜底要能同时吃 Paper 和 NewsItem。"""
        cand = Candidate(ref=1, channel="news", record=make_news(title="A report"), score=1.0)
        digest = rule_based_digest([cand], settings)
        assert digest.items[0].title_en == "A report"

    def test_invalid_discipline_falls_back(self, settings):
        paper = make_paper(work_id="W1", discipline="物理")
        cand = Candidate(ref=1, channel="paper", record=paper, score=1.0)
        digest = rule_based_digest([cand], settings)
        assert digest.items[0].discipline in ("综合", "医学", "计算机", "数学与计算数学")
