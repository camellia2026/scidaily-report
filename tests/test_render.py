"""反幻觉闸门与渲染。

★ 其中 TestSmallSampleGate 直接对应原项目的一次真实事故：
  某天只有 1 条候选，占位摘要被判「1/1 过短 = 100%」，
  整份报告被误判为不合格、运行变红。
"""

from __future__ import annotations

from scidaily.ai import NO_ABSTRACT, Digest, DigestItem
from scidaily.filter import Candidate
from scidaily.render import (
    GroundingReport,
    SECTIONS,
    _clean_url,
    _group_by_section,
    check_urls,
    render_html,
    render_markdown,
    sanitize,
)

from conftest import make_paper


def _cand(ref: int, channel: str = "paper", title: str = "A Study"):
    rec = make_paper(title=title, work_id=f"W{ref}")
    return Candidate(ref=ref, channel=channel, record=rec, score=1.0)


def _digest(items) -> Digest:
    return Digest(overview_zh="综述。", items=items, watchlist=[])


class TestSanitizeOutOfRange:
    def test_drops_item_with_out_of_range_ref(self):
        cands = [_cand(1), _cand(2)]
        d = _digest([
            DigestItem(refs=[1], title_en="A", headline_zh="甲", summary_zh="足够长的摘要内容" * 3),
            DigestItem(refs=[99], title_en="B", headline_zh="乙", summary_zh="足够长的摘要内容" * 3),
        ])
        cleaned, report = sanitize(d, cands, None)
        assert len(cleaned.items) == 1
        assert 99 in report.out_of_range
        assert report.dropped == 1

    def test_valid_items_all_survive(self):
        cands = [_cand(i) for i in (1, 2, 3)]
        d = _digest([
            DigestItem(
                refs=[i], title_en=f"T{i}", headline_zh=f"标{i}",
                # 每条摘要都不同，避免触发雷同检测
                summary_zh=f"这是第 {i} 条足够长的中文摘要内容，用来通过长度检查。",
            )
            for i in (1, 2, 3)
        ])
        cleaned, report = sanitize(d, cands, None)
        assert len(cleaned.items) == 3
        assert report.ok, report.problems

    def test_high_drop_ratio_fails_the_gate(self):
        """丢弃率过高说明模型整体没跟上 -> 判定失败、不推送。"""
        cands = [_cand(1)]
        d = _digest([
            DigestItem(refs=[1], title_en="ok", headline_zh="甲", summary_zh="足够长的摘要内容" * 3),
            *[
                DigestItem(refs=[900 + i], title_en="bad", headline_zh="乙", summary_zh="足够长的摘要内容" * 3)
                for i in range(5)
            ],
        ])
        _, report = sanitize(d, cands, None)
        assert not report.ok
        assert any("越界率" in p for p in report.problems)


class TestSmallSampleGate:
    """★ 单样本假阳性——原项目的真实事故。

    「1 条里 1 条过短 = 100%」会把整份报告判成不合格。
    修法是两条：占位符排除在检查外；样本 < 3 时不做比例判断。
    """

    def test_single_placeholder_summary_does_not_fail(self):
        cands = [_cand(1)]
        d = _digest([
            DigestItem(refs=[1], title_en="T", headline_zh="标", summary_zh=NO_ABSTRACT),
        ])
        _, report = sanitize(d, cands, None)
        assert report.ok, f"单条占位摘要不该判失败，实际：{report.problems}"

    def test_two_short_summaries_do_not_fail(self):
        cands = [_cand(1), _cand(2)]
        d = _digest([
            DigestItem(refs=[1], title_en="T1", headline_zh="甲", summary_zh="短"),
            DigestItem(refs=[2], title_en="T2", headline_zh="乙", summary_zh="短"),
        ])
        _, report = sanitize(d, cands, None)
        assert report.ok, f"样本 < 3 时不该做比例判断，实际：{report.problems}"

    def test_many_short_summaries_do_fail(self):
        cands = [_cand(i) for i in range(1, 6)]
        d = _digest([
            DigestItem(refs=[i], title_en=f"T{i}", headline_zh=f"标{i}", summary_zh="短")
            for i in range(1, 6)
        ])
        _, report = sanitize(d, cands, None)
        assert not report.ok

    def test_duplicate_summaries_fail(self):
        cands = [_cand(i) for i in range(1, 6)]
        same = "这是一段完全相同的摘要内容，用来触发雷同检测。"
        d = _digest([
            DigestItem(refs=[i], title_en=f"T{i}", headline_zh=f"标{i}", summary_zh=same)
            for i in range(1, 6)
        ])
        _, report = sanitize(d, cands, None)
        assert not report.ok
        assert any("雷同" in p for p in report.problems)


class TestUrlCleaning:
    """★ 实测踩到的坑：Cell 的链接含圆括号。

        https://www.cell.com/cell/fulltext/S0092-8674(26)00845-1

    正则排除 `)` 的话会在第一个右括号处截断，得到一个
    **真实存在但不完整**的 URL —— 「URL 泄漏检查」把合法链接报成伪造链接，
    渲染出的锚点也指向 404。假阳性比漏报更费时间。
    """

    def test_keeps_balanced_parentheses(self):
        url = "https://www.cell.com/cell/fulltext/S0092-8674(26)00845-1"
        assert _clean_url(url) == url

    def test_strips_trailing_period(self):
        assert _clean_url("https://e.org/x.") == "https://e.org/x"

    def test_strips_trailing_cjk_punctuation(self):
        assert _clean_url("https://e.org/x。") == "https://e.org/x"

    def test_strips_unbalanced_closing_paren(self):
        # 句子末尾的括号是标点，不是 URL 的一部分
        assert _clean_url("https://e.org/x)") == "https://e.org/x"

    def test_check_urls_finds_leak(self):
        md = "见 https://example.org/a 和 https://evil.example/fake"
        leaks = check_urls(md, {"https://example.org/a"})
        assert leaks == ["https://evil.example/fake"]

    def test_check_urls_no_false_positive_on_cell_link(self):
        url = "https://www.cell.com/cell/fulltext/S0092-8674(26)00845-1"
        assert check_urls(f"见 {url}", {url}) == []


class TestSections:
    def test_section_comes_from_channel_not_model(self):
        """★ 不让模型决定分节。

        即使模型把一条预印本写得像正式论文，它也会被放回预印本那一节。
        """
        cands = [_cand(1, "preprint"), _cand(2, "news")]
        d = _digest([
            DigestItem(refs=[1], title_en="T1", headline_zh="甲", summary_zh="足够长的摘要内容" * 3),
            DigestItem(refs=[2], title_en="T2", headline_zh="乙", summary_zh="足够长的摘要内容" * 3),
        ])
        by_ref = {c.ref: c for c in cands}
        grouped = _group_by_section(d, by_ref)
        assert len(grouped["preprint"]) == 1
        assert len(grouped["news"]) == 1

    def test_all_channels_present_in_sections_constant(self):
        names = {name for name, _ in SECTIONS}
        assert names == {"upgrade", "hot", "paper", "preprint", "news"}

    def test_upgrade_section_renders_first(self):
        assert SECTIONS[0][0] == "upgrade"


class TestMarkdownRendering:
    def test_smoke(self, settings):
        cands = [_cand(1)]
        d = _digest([
            DigestItem(
                refs=[1], title_en="Deep Learning Works",
                headline_zh="深度学习有效", summary_zh="这是一段足够长的中文摘要。" * 3,
                discipline="计算机", importance=8,
            )
        ])
        md = render_markdown("2026-10-02", d, cands, settings, [], {})
        assert "# 每日科学前沿 · 2026-10-02" in md
        assert "## 今日综述" in md
        assert "## 顶刊论文" in md
        assert "Deep Learning Works" in md
        # 免责声明是认真的，不是装饰
        assert "原文链接为准" in md
        assert "未经同行评审" in md

    def test_empty_day_has_explicit_banner(self, settings):
        md = render_markdown("2026-10-02", _digest([]), [], settings, [], {}, is_empty_day=True)
        assert "今日无新条目" in md

    def test_grounding_failure_banner(self, settings):
        report = GroundingReport(ok=False, problems=["越界引用率 50% 超过阈值"])
        md = render_markdown(
            "2026-10-02", _digest([]), [], settings, [], {}, grounding=report
        )
        assert "校验未通过" in md


class TestHtmlRendering:
    def test_produces_standalone_html(self):
        html = render_html("# 标题\n\n正文", "2026-10-02")
        assert html.startswith("<!doctype html>")
        assert "<style" not in html.split("</head>")[0].replace("style='", "")
        assert "标题" in html

    def test_inline_styles_not_stylesheet(self):
        """邮件客户端会剥离 <style> 和外部样式表，所以必须内联。"""
        html = render_html("## 小标题\n\n- 项目一", "2026-10-02")
        assert "<style>" not in html
        assert "style=" in html

    def test_table_renders(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        html = render_html(md, "2026-10-02")
        assert "<table" in html and "<td" in html


class TestFeedStatusClassification:
    """「解析出 0 条」与「源今天没内容」必须区分开。

    实测踩到过：Nature 的 feed 有 139KB / 75 条，某次网络抖动让响应被截断，
    feedparser 安静地返回 0 条 —— 体检表上显示成 "empty"，
    看起来像「今天没论文」，实际是取数失败。
    """

    def test_truncated_body_is_parse_error(self, tmp_path, settings):
        import httpx

        from scidaily.sources import rss

        # 一个体量不小但解析不出条目的响应
        body = b"<rdf:RDF>" + b"x" * 5000

        def fake_get(url, ua, timeout_s, throttle_s):
            return body

        original = rss._http_get_feed
        rss._http_get_feed = fake_get
        try:
            journal = settings.journals[0]  # Nature，有 feed
            _, _, report = rss.fetch_journal_feed(journal, settings, ttl_hours=0.0)
            assert report.status == "parse_error", report.status
            assert "截断" in report.error
        finally:
            rss._http_get_feed = original

    def test_empty_body_is_empty_not_error(self, settings):
        from scidaily.sources import rss

        original = rss._http_get_feed
        rss._http_get_feed = lambda *a, **k: b""
        try:
            journal = settings.journals[0]
            _, _, report = rss.fetch_journal_feed(journal, settings, ttl_hours=0.0)
            assert report.status == "empty"
        finally:
            rss._http_get_feed = original
