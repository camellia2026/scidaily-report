"""跨天去重状态，以及「版本升级」这条唯一的例外。"""

from __future__ import annotations

import json
from pathlib import Path

from scidaily.state import (
    SeenState,
    load_state,
    normalize_doi,
    normalize_title,
    normalize_url,
    save_state,
)


class TestNormalization:
    def test_strips_tracking_params(self):
        a = normalize_url("https://example.org/x?utm_source=t&id=1")
        b = normalize_url("https://example.org/x?id=1&utm_medium=e")
        assert a == b

    def test_sorts_query_pairs(self):
        assert normalize_url("https://e.org/x?b=2&a=1") == normalize_url("https://e.org/x?a=1&b=2")

    def test_drops_fragment(self):
        assert normalize_url("https://e.org/x#sec") == normalize_url("https://e.org/x")

    def test_doi_prefixes_are_stripped(self):
        for raw in ("https://doi.org/10.1/x", "http://doi.org/10.1/x", "doi:10.1/x", "10.1/X"):
            assert normalize_doi(raw) == "10.1/x"

    def test_title_fingerprint_ignores_case_and_punctuation(self):
        assert normalize_title("Hello, World!") == normalize_title("hello world")

    def test_title_fingerprint_keeps_cjk(self):
        # 中英混排是常态，CJK 必须原样保留，否则不同论文会撞在一起
        assert normalize_title("深度 学习") == "深度学习"
        assert normalize_title("深度 学习") != normalize_title("强化 学习")


class TestSeenState:
    def test_mark_then_is_seen(self):
        s = SeenState()
        assert s.mark("2026-10-02", work_id="W1", doi="10.1/a", title="T")
        assert s.is_seen(work_id="W1")
        assert s.is_seen(doi="10.1/a")
        assert s.is_seen(title="T")

    def test_mark_is_idempotent(self):
        s = SeenState()
        assert s.mark("2026-10-02", work_id="W1")
        assert not s.mark("2026-10-03", work_id="W1")
        # 首次日期不被覆盖——它是 90 天裁剪的依据
        from scidaily.state import work_key

        assert s.seen[work_key("W1")].date == "2026-10-02"

    def test_mark_without_any_key_is_noop(self):
        s = SeenState()
        assert not s.mark("2026-10-02")
        assert s.seen == {}


class TestVenueUpgrade:
    """增强点 3 的另一半：预印本 → 正式发表要**报一次**。

    按「不重复上报」的铁律它该被静默，但那本身就是新闻。
    """

    def test_preprint_then_journal_is_upgrade(self):
        s = SeenState()
        s.mark("2026-10-01", work_id="W1", venue="preprint")
        assert s.is_venue_upgrade("W1", "journal")

    def test_journal_then_journal_is_not_upgrade(self):
        s = SeenState()
        s.mark("2026-10-01", work_id="W1", venue="journal")
        assert not s.is_venue_upgrade("W1", "journal")

    def test_upgrade_fires_only_once(self):
        s = SeenState()
        s.mark("2026-10-01", work_id="W1", venue="preprint")
        assert s.is_venue_upgrade("W1", "journal")
        # 上报之后必须改记录，否则下周会再报一次
        s.mark_upgraded("W1", "journal")
        assert not s.is_venue_upgrade("W1", "journal")

    def test_unknown_work_is_not_upgrade(self):
        assert not SeenState().is_venue_upgrade("W-nope", "journal")

    def test_preprint_venue_now_is_not_upgrade(self):
        s = SeenState()
        s.mark("2026-10-01", work_id="W1", venue="preprint")
        assert not s.is_venue_upgrade("W1", "preprint")

    def test_mark_promotes_venue_without_resetting_date(self):
        s = SeenState()
        s.mark("2026-10-01", work_id="W1", venue="preprint")
        s.mark("2026-10-02", work_id="W1", venue="journal")
        from scidaily.state import work_key

        entry = s.seen[work_key("W1")]
        assert entry.venue == "journal"
        assert entry.date == "2026-10-01"


class TestPrune:
    def test_drops_old_entries(self):
        s = SeenState()
        s.mark("2026-01-01", work_id="old")
        s.mark("2026-10-01", work_id="new")
        removed = s.prune("2026-10-02", days=90)
        assert removed == 1
        assert s.is_seen(work_id="new")
        assert not s.is_seen(work_id="old")


class TestPersistence:
    def test_roundtrip(self, tmp_path: Path):
        path = tmp_path / "seen.json"
        s = SeenState(last_report_date="2026-10-02", sent=True)
        s.mark("2026-10-02", work_id="W1", venue="preprint")
        save_state(s, path)

        back = load_state(path)
        assert back.last_report_date == "2026-10-02"
        assert back.sent is True
        assert back.is_seen(work_id="W1")
        assert back.is_venue_upgrade("W1", "journal")

    def test_corrupt_file_degrades_to_empty(self, tmp_path: Path):
        """状态文件坏掉不能让今早没有报告。"""
        path = tmp_path / "seen.json"
        path.write_text("{ this is not json", encoding="utf-8")
        s = load_state(path)
        assert s.seen == {}

    def test_v1_string_format_still_reads(self, tmp_path: Path):
        """兼容旧格式 {key: "2026-09-23"}。"""
        path = tmp_path / "seen.json"
        path.write_text(
            json.dumps({"version": 1, "seen": {"t:abc": "2026-09-23"}}), encoding="utf-8"
        )
        s = load_state(path)
        assert s.seen["t:abc"].date == "2026-09-23"
        assert s.seen["t:abc"].venue == "journal"

    def test_save_is_atomic_no_tmp_left_behind(self, tmp_path: Path):
        path = tmp_path / "seen.json"
        save_state(SeenState(), path)
        assert path.exists()
        assert not list(tmp_path.glob("*.tmp"))
