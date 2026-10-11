"""跨天去重状态与历史归档。

这就是「日报」和「信息洪流」的区别：没有它，同一篇论文天天出现在报告里。

与原项目的差异只有一处，但很关键：
  原项目的键是 url / title 指纹；
  本项目的键是 **OpenAlex work id**。
因为 OpenAlex 已经把同一篇论文的 arXiv 版和期刊版聚在同一个 work 里，
按 work id 去重 = 预印本与正式版自动合并（增强点 3 的地基）。

副产品是一个原项目没有的能力：**版本升级**。
一篇上周以预印本上报过的论文，本周正式发表在 Nature 上——
按「不重复上报」的规则它该被静默，但那本身就是新闻。
所以 seen 的每条额外记下「上次上报时的版本类型」，升级时放行一次。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from common.console import warn

from .settings import DATA_DIR

STATE_PATH = DATA_DIR / "seen.json"
HISTORY_DIR = DATA_DIR / "history"

# 追踪参数与查询串噪声。同一篇论文的链接会带 utm_* / ref / fbclid 等尾巴，
# 不剥掉的话同一条内容会被当成两条。
_TRACKING_PARAMS = re.compile(
    r"(^utm_|^ref$|^fbclid$|^gclid$|^mc_cid$|^mc_eid$|^from$|^share_|^spm$|^src$)",
    re.I,
)
_TITLE_NOISE = re.compile(r"[\s　\-_/\\|:：,，.。;；!！?？'\"“”‘’()（）\[\]【】<>《》]+")


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    url = url.split("#", 1)[0]
    if "?" not in url:
        return url.rstrip("/")
    base, _, query = url.partition("?")
    kept = [p for p in query.split("&") if p and not _TRACKING_PARAMS.match(p)]
    tail = "&".join(sorted(kept))
    return (base + ("?" + tail if tail else "")).rstrip("/")


def normalize_title(title: str) -> str:
    """标题指纹。

    ★ 只在「大小写 + 标点 + 空白」层面归一，不做词干化——
      过度归一（比如丢掉所有非字母字符）会让不同论文撞在一起。
      中英混排是常态，所以 CJK 字符原样保留。
    """
    return _TITLE_NOISE.sub("", (title or "").strip().lower())


def normalize_doi(doi: str) -> str:
    doi = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "https://dx.doi.org/"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi.strip()


def _digest(prefix: str, text: str) -> str:
    return prefix + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def work_key(work_id: str) -> str:
    return _digest("w:", (work_id or "").strip().lower())


def doi_key(doi: str) -> str:
    return _digest("d:", normalize_doi(doi))


def title_key(title: str) -> str:
    return _digest("t:", normalize_title(title))


def url_key(url: str) -> str:
    return _digest("u:", normalize_url(url))


@dataclass
class SeenEntry:
    """一条去重记录。

    date        首次见到它的日期（用于 90 天裁剪）
    venue       上次上报时的版本类型：preprint | journal
    """

    date: str
    venue: str = "journal"

    def to_dict(self) -> dict:
        return {"d": self.date, "v": self.venue}


@dataclass
class SeenState:
    version: int = 2
    last_success_at: str | None = None
    last_report_date: str | None = None
    last_weekly_date: str | None = None
    sent: bool = False
    seen: dict[str, SeenEntry] = field(default_factory=dict)
    # 仅内存：本次运行新标记了多少条
    newly_marked: int = 0

    # ---- 查询 ----
    def lookup(
        self, work_id: str = "", doi: str = "", title: str = "", url: str = ""
    ) -> SeenEntry | None:
        """按 work_id → doi → title → url 的优先级找记录。

        优先级是有意的：work_id 最可靠，title 与 url 最不可靠
        （不同论文可能同名；同一篇论文可能有多个 URL）。
        """
        for key in self._keys(work_id, doi, title, url):
            entry = self.seen.get(key)
            if entry is not None:
                return entry
        return None

    def is_seen(
        self, work_id: str = "", doi: str = "", title: str = "", url: str = ""
    ) -> bool:
        return self.lookup(work_id, doi, title, url) is not None

    def is_venue_upgrade(self, work_id: str, venue_now: str) -> bool:
        """这篇论文是否发生了「预印本 -> 正式发表」。

        只有当它**上次是以预印本身份上报的**、且现在有了期刊版，才算升级。
        已经是期刊版的再报一次不算。
        """
        if venue_now != "journal":
            return False
        entry = self.seen.get(work_key(work_id)) if work_id else None
        return entry is not None and entry.venue == "preprint"

    # ---- 写入 ----
    def mark(
        self,
        date: str,
        work_id: str = "",
        doi: str = "",
        title: str = "",
        url: str = "",
        venue: str = "journal",
    ) -> bool:
        """标记为已见。幂等：已存在则只更新 venue（用于记录升级），返回 False。"""
        keys = self._keys(work_id, doi, title, url)
        if not keys:
            return False
        for key in keys:
            existing = self.seen.get(key)
            if existing is not None:
                # 已见过：只允许把 preprint 升级成 journal，不覆盖首次日期
                if existing.venue == "preprint" and venue == "journal":
                    existing.venue = "journal"
                return False
        for key in keys:
            self.seen[key] = SeenEntry(date=date, venue=venue)
        self.newly_marked += 1
        return True

    def mark_upgraded(self, work_id: str, venue: str = "journal") -> None:
        """升级上报之后，把记录里的版本改成新版，避免下周再报一次。"""
        key = work_key(work_id)
        if key in self.seen:
            self.seen[key].venue = venue

    @staticmethod
    def _keys(work_id: str = "", doi: str = "", title: str = "", url: str = "") -> list[str]:
        out: list[str] = []
        if work_id:
            out.append(work_key(work_id))
        if doi:
            out.append(doi_key(doi))
        if title:
            out.append(title_key(title))
        if url:
            out.append(url_key(url))
        return out

    # ---- 维护 ----
    def prune(self, today: str, days: int) -> int:
        """丢掉超过 days 天的记录。

        ★ 状态文件随天数线性增长（约 150 条/天 → 一年约 5 万条）。
          90 天裁剪后稳态约 1.4 万条，每天提交进 git 完全可接受。
        """
        cutoff = _shift_date(today, -days)
        dead = [k for k, e in self.seen.items() if e.date < cutoff]
        for k in dead:
            del self.seen[k]
        return len(dead)

    def staleness_hours(self, now: datetime) -> float | None:
        if not self.last_success_at:
            return None
        try:
            last = datetime.fromisoformat(self.last_success_at)
        except ValueError:
            return None
        return (now - last).total_seconds() / 3600.0

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "last_success_at": self.last_success_at,
            "last_report_date": self.last_report_date,
            "last_weekly_date": self.last_weekly_date,
            "sent": self.sent,
            "seen": {k: e.to_dict() for k, e in self.seen.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SeenState":
        state = cls(
            version=int(raw.get("version", 2)),
            last_success_at=raw.get("last_success_at"),
            last_report_date=raw.get("last_report_date"),
            last_weekly_date=raw.get("last_weekly_date"),
            sent=bool(raw.get("sent", False)),
        )
        for key, value in (raw.get("seen") or {}).items():
            if isinstance(value, str):
                # v1 兼容：旧格式是 {key: "2026-09-23"}
                state.seen[key] = SeenEntry(date=value, venue="journal")
            elif isinstance(value, dict):
                state.seen[key] = SeenEntry(
                    date=str(value.get("d", "")), venue=str(value.get("v", "journal"))
                )
        return state


def _shift_date(date: str, days: int) -> str:
    from datetime import timedelta

    try:
        return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return "0000-00-00"


def load_state(path: Path | None = None) -> SeenState:
    """读状态。损坏时降级为空状态并告警，绝不因为状态文件坏掉就让今早没有报告。"""
    path = path or STATE_PATH
    if not path.exists():
        return SeenState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        warn(f"状态文件 {path.name} 读不出来（{e}），本次按空状态运行。")
        return SeenState()
    if not isinstance(raw, dict):
        warn(f"状态文件 {path.name} 顶层不是对象，本次按空状态运行。")
        return SeenState()
    return SeenState.from_dict(raw)


def save_state(state: SeenState, path: Path | None = None) -> Path:
    """原子写：先写 .tmp 再 replace。

    崩在中间也不能留下半个 JSON——那会让明天的去重读不出来，
    一条坏状态会连锁污染之后所有天。
    """
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------
# 历史归档：给程序看的双胞胎
# --------------------------------------------------------------------------


def history_path(date: str) -> Path:
    return HISTORY_DIR / f"{date}.json"


def history_exists(date: str) -> bool:
    return history_path(date).exists()


def save_history(date: str, payload: dict) -> Path:
    path = history_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def load_history(date: str) -> dict | None:
    path = history_path(date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
