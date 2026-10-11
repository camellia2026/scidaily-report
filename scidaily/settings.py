"""配置加载。

本模块是「调行为」的唯一入口：改 config/*.yaml 不用动代码。
敏感信息（LLM key、邮箱授权码）不在这里，在 .env / GitHub Secrets。

★ 全项目唯一的日期来源是 report_date()。任何地方都不要裸用 datetime.now()。
  理由见原项目 2026-09-25 事故：GitHub Actions 的 cron 会延迟，跨过 UTC 午夜时
  用错日期会把报告归档到错误的一天，同时污染去重窗口。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

VALID_TIERS = ("T0", "T1", "T2")
VALID_DISCIPLINES = ("综合", "医学", "计算机", "数学与计算数学")
VALID_ROUTES = ("openalex", "crossref", "pubmed", "rss")
VALID_KINDS = ("news", "paper")


class SettingsError(Exception):
    """配置有问题。消息里必须写清楚「怎么修」。"""


# --------------------------------------------------------------------------
# 期刊
# --------------------------------------------------------------------------


@dataclass
class Journal:
    """一个被追踪的期刊。

    route 决定用哪些采集通道去取它的论文：
      openalex  主力。带 locations 聚类与 counts_by_year，且能一次 OR 多 ISSN。
      crossref  补漏。无摘要，仅在 OpenAlex 缺条目时用。
      pubmed    医学刊摘要的最佳来源。
      rss       期刊自带的 TOC feed（只有部分刊有）。
    """

    id: str
    name: str
    issn: str
    tier: str = "T2"
    discipline: str = "综合"
    route: list[str] = field(default_factory=lambda: ["openalex"])
    feed: str = ""
    # feed 里既有研究论文也有新闻稿时，用 DOI 前缀区分。
    # 例如 Nature：10.1038/s41586- 是论文，10.1038/d41586- 是新闻。
    research_doi_prefix: str = ""
    note: str = ""

    @property
    def tier_weight(self) -> float:
        """分级权重，进预打分。T0 顶刊，T1 一级，T2 二级。"""
        return {"T0": 3.0, "T1": 2.0, "T2": 1.4}.get(self.tier, 1.0)

    def is_research_doi(self, doi: str) -> bool:
        """该 DOI 是否属于本刊的研究论文（相对于新闻/社论/通讯）。

        前缀支持逗号分隔的多值，因为同一刊的研究论文往往有多个 DOI 段。
        例如 NEJM：10.1056/NEJMoa（原创论文）、10.1056/NEJMra（综述）。

        前缀为空 = 该 feed 里全是研究论文，不做区分。
        没有 DOI 时保守地判为**不是**研究论文——feed 里真正的研究论文
        几乎总有 DOI，没有的那个更可能是新闻稿。
        """
        if not self.research_doi_prefix:
            return True
        if not doi:
            return False
        lowered = doi.lower()
        return any(
            lowered.startswith(p.strip().lower())
            for p in self.research_doi_prefix.split(",")
            if p.strip()
        )


# --------------------------------------------------------------------------
# 新闻源
# --------------------------------------------------------------------------


@dataclass
class Source:
    """一个 RSS/Atom 新闻源。

    ★ user_agent 的三态是原项目用全量对照实验换来的教训：
      实测中 syncedreview / marktechpost 带 UA 反而 403，mit_csail 不带 UA 才 403。
      没有可以推广的默认值，必须逐源实测。
        browser  = 发浏览器 UA
        identify = 发带项目名的 UA
        none     = 一个 UA 头都不发
    """

    id: str
    name: str
    url: str
    kind: str = "news"
    weight: float = 1.0
    enabled: bool = True
    environments: list[str] = field(default_factory=lambda: ["local", "ci"])
    user_agent: str = "browser"
    throttle_s: float = 0.0
    note: str = ""

    @property
    def ua(self) -> str | None:
        if self.user_agent == "none":
            return None
        if self.user_agent == "identify":
            return IDENTIFY_UA
        return BROWSER_UA


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IDENTIFY_UA = "scidaily-report/0.1 (+https://github.com/; mailto:see .env SCIDaily_CONTACT)"


# --------------------------------------------------------------------------
# 运行设置
# --------------------------------------------------------------------------


@dataclass
class Settings:
    journals: list[Journal]
    sources: list[Source]
    windows: dict
    report: dict
    channels: dict
    prefilter: dict
    velocity: dict
    preprints: dict
    state: dict
    notify: dict
    api: dict
    timezone: str

    # ---- 派生属性：把配置字典里的键名收敛到一处，调用方不用记拼写 ----
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def news_window_hours(self) -> float:
        return float(self.windows.get("news", 24))

    @property
    def paper_window_hours(self) -> float:
        return float(self.windows.get("paper", 72))

    @property
    def weekly_window_days(self) -> int:
        return int(self.windows.get("weekly_days", 14))

    @property
    def weekly_fallback_days(self) -> int:
        """慢刊（JMLR 不定期、Acta Numerica 年刊）在小窗口内可能一条都没有。

        周度盘点若一无所获，就把回看窗口放宽到这个值，而不是交一张白卷。
        """
        return int(self.windows.get("weekly_fallback_days", 90))

    @property
    def max_candidates(self) -> int:
        return int(self.prefilter.get("max_candidates", 60))

    @property
    def prune_days(self) -> int:
        return int(self.state.get("prune_days", 90))

    def channel_cap(self, channel: str) -> int:
        """某通道送进 LLM 的候选上限。"""
        return int(self.channels.get(channel, {}).get("max_candidates", 20))

    def channel_quota(self, channel: str) -> int:
        """某通道在报告里最终占的条数。"""
        return int(self.channels.get(channel, {}).get("quota", 5))


def load_env() -> None:
    """读 .env。对不存在的文件不报错——CI 上就是没有 .env 的。"""
    load_dotenv(ROOT / ".env")


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise SettingsError(f"找不到配置文件 {path}。请确认 config/ 目录完整。")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SettingsError(f"{path} 不是合法 YAML：{e}") from e
    if not isinstance(raw, dict):
        raise SettingsError(f"{path} 的顶层必须是映射（key: value），实际是 {type(raw).__name__}。")
    return raw


def runtime_env() -> str:
    """local | ci。决定哪些源参与采集。"""
    return os.getenv("SCIDAILY_ENV", os.getenv("AI_DAILY_ENV", "local")).strip().lower() or "local"


def load_journals() -> list[Journal]:
    raw = _read_yaml(CONFIG_DIR / "journals.yaml")
    items = raw.get("journals") or []
    if not items:
        raise SettingsError("config/journals.yaml 里没有任何期刊。至少要有一行 journals:。")

    out: list[Journal] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or "id" not in item:
            raise SettingsError(f"config/journals.yaml 第 {i + 1} 个期刊缺少 id 字段。")
        jid = str(item["id"])
        issn = str(item.get("issn", "")).strip()
        if not _looks_like_issn(issn):
            raise SettingsError(
                f"期刊 {jid!r} 的 ISSN {issn!r} 格式不对。"
                "应形如 0028-0836（XXXX-XXXX，数字，最后一位可为 X）。"
            )
        tier = str(item.get("tier", "T2"))
        if tier not in VALID_TIERS:
            raise SettingsError(
                f"期刊 {jid!r} 的 tier={tier!r} 无效。可选：{'、'.join(VALID_TIERS)}。"
            )
        disc = str(item.get("discipline", "综合"))
        if disc not in VALID_DISCIPLINES:
            raise SettingsError(
                f"期刊 {jid!r} 的 discipline={disc!r} 无效。可选：{'、'.join(VALID_DISCIPLINES)}。"
            )
        route = [str(r) for r in (item.get("route") or ["openalex"])]
        bad = [r for r in route if r not in VALID_ROUTES]
        if bad:
            raise SettingsError(
                f"期刊 {jid!r} 的 route 含未知通道 {bad}。可选：{'、'.join(VALID_ROUTES)}。"
            )
        out.append(
            Journal(
                id=jid,
                name=str(item.get("name", jid)),
                issn=issn,
                tier=tier,
                discipline=disc,
                route=route,
                feed=str(item.get("feed", "")),
                research_doi_prefix=str(item.get("research_doi_prefix", "")),
                note=str(item.get("note", "")).strip(),
            )
        )
    return out


def _looks_like_issn(value: str) -> bool:
    if len(value) != 9 or value[4] != "-":
        return False
    body = value[:4] + value[5:]
    return all(c.isdigit() for c in body[:7]) and (body[7].isdigit() or body[7] in "Xx")


def load_sources(env: str | None = None, include_disabled: bool = False) -> list[Source]:
    raw = _read_yaml(CONFIG_DIR / "sources.yaml")
    items = raw.get("sources") or []
    env = env or runtime_env()

    out: list[Source] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or "id" not in item or "url" not in item:
            raise SettingsError(f"config/sources.yaml 第 {i + 1} 个源缺少 id 或 url 字段。")
        kind = str(item.get("kind", "news"))
        if kind not in VALID_KINDS:
            raise SettingsError(
                f"源 {item['id']!r} 的 kind={kind!r} 无效。可选：{'、'.join(VALID_KINDS)}。"
            )
        src = Source(
            id=str(item["id"]),
            name=str(item.get("name", item["id"])),
            url=str(item["url"]),
            kind=kind,
            weight=float(item.get("weight", 1.0)),
            enabled=bool(item.get("enabled", True)),
            environments=[str(e) for e in (item.get("environments") or ["local", "ci"])],
            user_agent=str(item.get("user_agent", "browser")),
            throttle_s=float(item.get("throttle_s", 0.0)),
            note=str(item.get("note", "")).strip(),
        )
        if not include_disabled and not src.enabled:
            continue
        if env not in src.environments:
            continue
        out.append(src)
    return out


def load_settings() -> Settings:
    raw = _read_yaml(CONFIG_DIR / "settings.yaml")
    notify = raw.get("notify") or {}
    api = raw.get("api") or {}

    api.setdefault("mailto", os.getenv("SCIDAILY_CONTACT", "").strip())
    api.setdefault("openalex_base", "https://api.openalex.org")
    api.setdefault("crossref_base", "https://api.crossref.org")
    api.setdefault("pubmed_base", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils")
    api.setdefault("pubmed_min_interval_s", 0.34)   # NCBI：无 key 时 3 req/s
    api.setdefault("arxiv_min_interval_s", 3.0)    # arXiv 官方要求 ≥3s
    api.setdefault("timeout_s", 45.0)
    api.setdefault("offline_fixtures", str(ROOT / "tests" / "fixtures" / "api"))

    return Settings(
        journals=load_journals(),
        sources=load_sources(),
        windows=raw.get("windows") or {},
        report=raw.get("report") or {},
        channels=raw.get("channels") or {},
        prefilter=raw.get("prefilter") or {},
        velocity=raw.get("velocity") or {},
        preprints=raw.get("preprints") or {},
        state=raw.get("state") or {},
        notify=notify,
        api=api,
        timezone=str((raw.get("report_meta") or {}).get("timezone", "Asia/Shanghai")),
    )


# --------------------------------------------------------------------------
# 日期：全项目唯一来源
# --------------------------------------------------------------------------


def now_local(settings: Settings, when: datetime | None = None) -> datetime:
    if when is not None:
        return when.astimezone(settings.tz) if when.tzinfo else when.replace(tzinfo=settings.tz)
    return datetime.now(settings.tz)


def report_date(settings: Settings, when: datetime | None = None) -> str:
    """本报告归属的日期，YYYY-MM-DD。★ 全项目唯一的日期来源。"""
    return now_local(settings, when).strftime("%Y-%m-%d")


def window_start(settings: Settings, hours: float, when: datetime | None = None) -> datetime:
    return now_local(settings, when) - timedelta(hours=hours)


# --------------------------------------------------------------------------
# 邮件
# --------------------------------------------------------------------------


@dataclass
class MailConfig:
    host: str
    port: int
    username: str
    auth_code: str
    to: str

    @property
    def missing(self) -> list[str]:
        pairs = [
            ("MAIL_USERNAME", self.username),
            ("MAIL_AUTH_CODE", self.auth_code),
            ("MAIL_TO", self.to),
        ]
        return [name for name, value in pairs if not value]


def get_mail_config() -> MailConfig:
    """自己调用 load_env()，调用方就不会因为忘了初始化而读到空值。

    （原项目的真实 bug：get_mail_config() 只读 os.getenv，依赖调用方先跑 load_env。）
    """
    load_env()
    cfg = MailConfig(
        host=os.getenv("MAIL_SMTP_HOST", "smtp.qq.com"),
        port=int(os.getenv("MAIL_SMTP_PORT", "465") or 465),
        username=os.getenv("MAIL_USERNAME", "").strip(),
        auth_code=os.getenv("MAIL_AUTH_CODE", "").strip(),
        to=os.getenv("MAIL_TO", "").strip(),
    )
    if not cfg.to:
        cfg.to = cfg.username
    return cfg
