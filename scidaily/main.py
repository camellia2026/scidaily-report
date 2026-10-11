"""编排 + CLI。

五段流水线：
    1 采集   期刊(OpenAlex) + 要闻(RSS) + 热文候选 + 预印本 + 医学刊(PubMed)
    2 过滤   合并(预印本<->正式版) -> 去重 -> 分级分区 -> 预打分 -> 分通道候选
    3 生成   LLM -> 结构化 Digest（或规则兜底）
    4 校验   ★ 反幻觉闸门
    5 渲染推送 md / json / html -> 邮件

退出码（与原项目一致，且「送不出去 = 失败」是刻意的）：
    0   成功，或幂等跳过
    1   失败：所有源都挂了 / 校验未通过 / 推送失败 / --require-llm 下 LLM 失败
    130 键盘中断

★ 「报告生成了但没送出去」返回 1 而不是 0。
  原项目为此付出过代价：2026-09-25 的两次运行都是绿的，但一封邮件都没送达。
  绿勾和「今天没新闻」必须长得不一样。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import timedelta

from common.console import banner, err, fix_console, info, log, ok, warn
from common.structured import StructuredError

from . import ai, notify as notify_mod, preflight, render, velocity
from .filter import FilterStats, build_candidates, mark_seen
from .settings import (
    REPORTS_DIR,
    SettingsError,
    load_env,
    load_settings,
    now_local,
    report_date,
)
from .sources import cache, crossref, merge, openalex, pubmed, rss
from .sources.base import NewsItem, Paper, SourceReport
from .state import (
    history_exists,
    load_state,
    normalize_doi,
    save_history,
    save_state,
)


@dataclass
class Collected:
    papers: list[Paper] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)
    hot: list[Paper] = field(default_factory=list)
    preprints: list[Paper] = field(default_factory=list)
    reports: list[SourceReport] = field(default_factory=list)


# --------------------------------------------------------------------------
# 1. 采集
# --------------------------------------------------------------------------


def collect(
    settings,
    *,
    offline: bool,
    ttl_hours: float,
    limit: int | None = None,
    window_hours: float | None = None,
) -> Collected:
    """按通道采集。任何一个源失败都只记一行，绝不中止。

    window_hours：覆盖论文窗口。周度盘点传 14*24，这样它就复用与日报
    完全相同的采集路径——包括 PubMed 的 PublicationType 过滤。
    没有这个覆盖的话，盘点得自己拼一遍采集，就会漏掉那些过滤规则
    （实测后果：JAMA 的新闻稿和播客混进了盘点列表）。
    """
    out = Collected()
    now = now_local(settings)
    paper_hours = window_hours if window_hours is not None else settings.paper_window_hours

    # 期刊论文（新鲜）
    since_paper = now - timedelta(hours=paper_hours)
    papers, rep = openalex.fetch_journals(settings.journals, settings, since=since_paper,
                                          offline=offline, ttl_hours=ttl_hours)
    out.papers.extend(papers)
    out.reports.append(rep)

    # 期刊 TOC feed：补 OpenAlex 滞后的条目 + 提供编辑摘要
    for journal in settings.journals:
        if "rss" not in journal.route or not journal.feed:
            continue
        feed_papers, feed_news, frep = rss.fetch_journal_feed(
            journal, settings, offline=offline, ttl_hours=ttl_hours
        )
        out.papers.extend(feed_papers)
        out.news.extend(feed_news)
        out.reports.append(frep)

    # 医学刊：PubMed 摘要是最好的，也是 JAMA 的唯一抓手
    for journal in settings.journals:
        if "pubmed" not in journal.route:
            continue
        p_med, mrep = pubmed.fetch_journal(
            journal, settings, since=since_paper, offline=offline, ttl_hours=ttl_hours
        )
        out.papers.extend(p_med)
        out.reports.append(mrep)

    # ★ 用 PubMed 的 PublicationType 给 OpenAlex 排雷。
    #   为什么非这样不可：JAMA/NEJM 的新闻稿、社论、通讯、播客在 OpenAlex 里
    #   type=article、type_crossref 又是空的——**OpenAlex 根本没有分辨它们的信息**。
    #   只有 PubMed 的 PublicationType 是权威的。
    #
    #   ★ 方向很重要：这里只**排除被明确判定为非研究**的 DOI，
    #     绝不用 PubMed 去**决定保留哪些**。
    #     后者会误删正经论文——PubMed 的 [EDAT]（入库日）与 OpenAlex 的
    #     publication_date（出版日）不是一回事，窗口错配会把
    #     刚上线但早已入库的论文判成「PubMed 不认识」。
    #     这也是实测踩到的：NEJM 的原始论文因此被整批删掉过。
    #
    #   窗口刻意放宽到 180 天（覆盖日报与周报），拿不到名单就不排雷。
    non_research: set[str] = set()
    block_since = now - timedelta(days=180)
    for journal in settings.journals:
        if "pubmed" not in journal.route:
            continue
        non_research |= pubmed.fetch_non_research_dois(
            journal, settings, since=block_since, offline=offline, ttl_hours=ttl_hours
        )
    if non_research:
        before = len(out.papers)
        out.papers = [
            p for p in out.papers
            if not (p.doi and normalize_doi(p.doi) in non_research)
        ]
        dropped = before - len(out.papers)
        if dropped:
            log("采集", f"PubMed 判定为非研究内容，排除 {dropped} 条（新闻稿/社论/通讯/播客）")

    # Crossref 补漏
    for journal in settings.journals:
        if "crossref" not in journal.route:
            continue
        c_papers, crep = crossref.fetch_journal(
            journal, settings, since=since_paper, offline=offline, ttl_hours=ttl_hours
        )
        out.papers.extend(c_papers)
        out.reports.append(crep)

    # 热文候选（近两年高引）+ 预印本
    hot, hrep = openalex.fetch_hot_journal_papers(
        settings.journals, settings,
        months=int(settings.velocity.get("max_age_months", 24)),
        limit=int(settings.velocity.get("shortlist", 10)) * 6,
        offline=offline, ttl_hours=ttl_hours,
    )
    out.hot.extend(hot)
    out.reports.append(hrep)

    pre, prep = openalex.fetch_notable_preprints(
        settings,
        days=int(settings.preprints.get("window_days", 60)),
        min_citations=int(settings.preprints.get("min_citations", 5)),
        limit=int(settings.preprints.get("limit", 60)),
        offline=offline, ttl_hours=ttl_hours,
    )
    out.preprints.extend(pre)
    out.reports.append(prep)

    # 科学要闻
    sources = settings.sources
    if limit is not None:
        sources = sources[:limit]
    news, nreps = rss.collect_news(sources, settings, offline=offline, ttl_hours=ttl_hours)
    out.news.extend(news)
    out.reports.extend(nreps)

    log("采集",
        f"论文 {len(out.papers)} · 热文候选 {len(out.hot)} · 预印本 {len(out.preprints)} "
        f"· 要闻 {len(out.news)}")
    return out


# --------------------------------------------------------------------------
# 2. 过滤
# --------------------------------------------------------------------------


def build_all_candidates(collected: Collected, state, settings, *, ignore_seen: bool):
    """把采集结果变成五组候选。返回 (候选, 每通道统计, 升级条目)。"""
    now = now_local(settings)
    stats: dict[str, FilterStats] = {}

    # 期刊论文：先合并（预印本<->正式版），再去重打分
    papers, merged_away = merge.dedupe_in_run(list(collected.papers))
    if merged_away:
        log("合并", f"预印本<->正式版合并掉了 {merged_away} 条重复")

    upgrades = merge.find_venue_upgrades(papers, state)

    paper_cands, paper_stats = build_candidates(
        papers, state, settings, now,
        channel="paper", max_age_hours=settings.paper_window_hours,
        ignore_seen=ignore_seen,
    )
    stats["paper"] = paper_stats

    # 热文：只取**被追踪期刊**的论文，按 hotness 排（近年引用 x 近年占比）。
    # ★ 预印本不进这个通道。它们的身份是「未经同行评审」，混进来会被
    #   当成正式发表的论文读；它们有自己的通道，且会明确标注。
    ranked = velocity.rank_hot(list(collected.hot), settings, now=now)
    ranked = velocity.refine_with_recent_window(ranked, settings, now=now, offline=False)
    hot_records = [p for p, _ in ranked]
    hot_cands, hot_stats = build_candidates(
        hot_records, state, settings, now,
        channel="hot", max_age_hours=None, ignore_seen=ignore_seen,
    )
    stats["hot"] = hot_stats

    news_cands, news_stats = build_candidates(
        list(collected.news), state, settings, now,
        channel="news", max_age_hours=settings.news_window_hours,
        ignore_seen=ignore_seen,
    )
    stats["news"] = news_stats

    # 版本升级：绕过 seen（这是「不重复上报」的唯一例外），但只放行一次
    up_cands, up_stats = build_candidates(
        upgrades, state, settings, now,
        channel="upgrade", max_age_hours=None, ignore_seen=True,
    )
    stats["upgrade"] = up_stats

    # 预印本：同样按热度排（一篇上线两周被引 10 次的预印本，
    # 比昨天上线无人问津的那篇更值得看）
    pre_ranked = velocity.rank_hot(list(collected.preprints), settings, now=now)
    preprint_records = [p for p, _ in pre_ranked]
    pre_cands, pre_stats = build_candidates(
        preprint_records, state, settings, now,
        channel="preprint", max_age_hours=None, ignore_seen=ignore_seen,
    )
    stats["preprint"] = pre_stats

    all_cands = up_cands + hot_cands + paper_cands + pre_cands + news_cands

    # ★ 全局重编号。这一段不能省。
    #   每个通道内部都是从 1 开始编号的，直接拼起来会有五个 ref=1、
    #   五个 ref=2……而 ref 是模型与数据的唯一契约：
    #   渲染时按 ref 反查候选，重复的 ref 会让条目被分到错误的节，
    #   甚至让「越界引用」的判定失效（越界查得出来，错位查不出来）。
    for i, cand in enumerate(all_cands, start=1):
        cand.ref = i

    return all_cands, stats, upgrades


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scidaily-report",
        description="每日科学前沿报告：顶刊论文 + 科学要闻",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
常用组合：
  python run.py --smoke                 纯离线冒烟（读固件，不联网、不推送、不调模型）
  python run.py --no-llm --dry-run      真采集 + 规则打分，不调模型
  python run.py --check-sources         逐个源体检
  python run.py --seed                  抓一次固化成离线固件
  python run.py selftest                发一封测试邮件验证渠道
""",
    )
    p.add_argument("--provider", help="LLM 后端：mock | github | deepseek | ollama | rule")
    p.add_argument("--smoke", action="store_true", help="= --provider mock --offline --dry-run")
    p.add_argument("--offline", action="store_true", help="只读缓存，绝不联网")
    p.add_argument("--fresh", action="store_true", help="忽略缓存 TTL，强制重新抓取")
    p.add_argument("--force", action="store_true", help="忽略「今天已生成」，强制重跑")
    p.add_argument("--dry-run", "--no-send", dest="dry_run", action="store_true",
                   help="跑完不推送")
    p.add_argument("--no-llm", action="store_true", help="只用规则，不调模型")
    p.add_argument("--require-llm", action="store_true", help="LLM 失败时不要规则兜底，直接 exit 1")
    p.add_argument("--ignore-seen", action="store_true",
                   help="忽略历史去重；隐含 --force，且**不写状态**（本地调试用）")
    p.add_argument("--limit", type=int, help="只跑前 N 个新闻源（调试用）")
    p.add_argument("--exclude", help="逗号分隔的源 id，跳过它们")
    p.add_argument("--alert", metavar="MSG", help="发一条失败告警，然后退出")

    sub = p.add_subparsers(dest="command")
    sub.add_parser("check-sources", help="逐个源体检")
    seed = sub.add_parser("seed", help="抓一次并固化成离线固件")
    seed.add_argument("--fresh", action="store_true", help="忽略缓存 TTL")
    st = sub.add_parser("selftest", help="发一封测试邮件")
    st.add_argument("--channel", default=None)
    # ★ 子命令要自带这些开关。argparse 的全局 flag 必须写在子命令**之前**，
    #   而 `weekly --force` 这种写法更符合直觉。两边都定义一份，
    #   子命令里的会覆盖全局的（同名 dest）。
    weekly = sub.add_parser("weekly", help="每周顶刊盘点（慢刊回看）")
    weekly.add_argument("--force", action="store_true", help="忽略「本周已生成」")
    weekly.add_argument("--dry-run", action="store_true", help="只跑不写状态")
    weekly.add_argument("--offline", action="store_true", help="只读缓存")
    return p


def main(argv: list[str] | None = None) -> int:
    fix_console()
    argv = list(sys.argv[1:] if argv is None else argv)

    # --smoke 是最常用的组合，展开成三个开关，省得每次都敲
    if "--smoke" in argv:
        argv = [a for a in argv if a != "--smoke"]
        argv = ["--provider", "mock", "--offline", "--dry-run"] + argv

    # 子命令的两种写法都接受：`--check-sources` 与 `check-sources`。
    # 带连字符的写法更符合命令行直觉（和原项目的文档一致），
    # 而不带连字符是 argparse 子命令的自然形式。两个都留，别让人猜。
    _SUBCOMMAND_ALIASES = {
        "--check-sources": "check-sources",
        "--seed": "seed",
        "--selftest": "selftest",
        "--weekly": "weekly",
    }
    for i, token in enumerate(argv):
        if token in _SUBCOMMAND_ALIASES:
            argv[i] = _SUBCOMMAND_ALIASES[token]
            break

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.ignore_seen:
        args.force = True

    try:
        if args.alert:
            return cmd_alert(args)
        if args.command == "check-sources":
            return cmd_check_sources(args)
        if args.command == "seed":
            return cmd_seed(args)
        if args.command == "selftest":
            return cmd_selftest(args)
        if args.command == "weekly":
            return cmd_weekly(args)
        return cmd_run(args)
    except KeyboardInterrupt:
        err("被中断")
        return 130
    except (SettingsError, preflight.PreflightError) as e:
        err(str(e))
        return 1


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def cmd_run(args) -> int:
    load_env()
    try:
        settings = load_settings()
    except SettingsError as e:
        err(str(e))
        return 1

    provider = args.provider or os.getenv("LLM_PROVIDER", "mock")
    use_llm = not args.no_llm and provider != "rule"
    will_push = not args.dry_run

    state = load_state()
    today = report_date(settings)
    now = now_local(settings)

    # ---- 幂等闸门：已生成**且**已推送才算完成 ----
    if not args.force and state.last_report_date == today and state.sent:
        ok(f"{today} 的报告已生成并推送过，跳过（用 --force 强制重跑）")
        return 0

    # ---- 启动自检，放在多秒采集之前 ----
    try:
        preflight.run_or_raise(
            provider=provider,
            fallback=os.getenv("LLM_FALLBACK_PROVIDER") or None,
            channel=settings.notify.get("channel", "email"),
            will_push=will_push,
            use_llm=use_llm,
        )
    except preflight.PreflightError as e:
        err(str(e))
        return 1

    # ---- 报告已存在：只重试推送，绝不重新生成 ----
    # 兜底 cron 触发时若重新生成，会用「全都已见」的空报告覆盖掉早上的好报告。
    if not args.force and history_exists(today):
        info("今日报告已存在，只重试推送")
        return _repush(today, settings, state, args)

    # ---- 1/5 采集 ----
    banner("1/5 采集")
    ttl = 0.0 if (args.fresh or args.offline) else cache.cache_ttl(
        os.getenv("SCIDAILY_ENV", "local")
    )
    collected = collect(settings, offline=args.offline, ttl_hours=ttl, limit=args.limit)

    healthy = [r for r in collected.reports if r.ok]
    if not healthy:
        err("所有数据源都失败了。这是一次真正的故障，不是「今天没新闻」。")
        for r in collected.reports:
            err(f"  {r.name}: {r.status} {r.error[:100]}")
        return 1
    if len(healthy) < len(collected.reports):
        warn(f"{len(collected.reports) - len(healthy)}/{len(collected.reports)} 个源失败")

    # ---- 2/5 过滤 ----
    banner("2/5 过滤与打分")
    candidates, stats, upgrades = build_all_candidates(
        collected, state, settings, ignore_seen=args.ignore_seen
    )
    for ch in ("upgrade", "hot", "paper", "preprint", "news"):
        st = stats.get(ch)
        if st and st.fetched:
            info(f"  {ch}: {st.summary_line()}")

    ref_to_channel = {c.ref: c.channel for c in candidates}
    del ref_to_channel

    # ---- 3/5 生成 ----
    banner("3/5 生成简报")
    backend = "rule"
    if not candidates:
        digest = ai.rule_based_digest([], settings)
        backend = "empty"
    elif not use_llm:
        digest = ai.rule_based_digest(candidates, settings)
    else:
        try:
            chain = ai.build_client_chain(provider)
            digest, backend = ai.analyze(candidates, settings, chain=chain)
        except (RuntimeError, StructuredError) as e:
            if args.require_llm:
                err(f"LLM 失败且指定了 --require-llm：{e}")
                return 1
            warn(f"LLM 失败，降级为规则模式：{e}")
            digest = ai.rule_based_digest(candidates, settings)
            backend = "rule-fallback"

    # ---- 4/5 校验 ----
    banner("4/5 反幻觉校验")
    digest, grounding = render.sanitize(digest, candidates, settings)
    info(grounding.describe())

    staleness = state.staleness_hours(now)

    # ---- 5/5 渲染 ----
    banner("5/5 渲染与推送")
    is_empty = not digest.items
    markdown = render.render_markdown(
        today, digest, candidates, settings, collected.reports, stats,
        grounding=grounding, backend=backend, staleness_hours=staleness, is_empty_day=is_empty,
    )

    # 两侧都要用同一套归一，否则含括号的合法链接（Cell 那种）会被误报
    known_urls = {render._clean_url(getattr(c.record, "url", "")) for c in candidates}
    leaks = render.check_urls(markdown, known_urls)
    if leaks:
        grounding.url_leaks = leaks
        warn(f"报告里有 {len(leaks)} 个本次未抓到的链接")
        markdown = render.render_markdown(
            today, digest, candidates, settings, collected.reports, stats,
            grounding=grounding, backend=backend, staleness_hours=staleness, is_empty_day=is_empty,
        )

    reports_dir = REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{today}.md").write_text(markdown, encoding="utf-8")
    ok(f"报告已写入 reports/{today}.md")

    sent = False
    should_push = will_push and not (is_empty and not settings.notify.get("send_on_empty", False))
    if should_push and not grounding.ok:
        warn("校验未通过：只归档，不推送，并返回 1")
        should_push = False

    if should_push:
        try:
            subject = f"{settings.notify.get('subject_prefix', '[科学前沿]')} {today}"
            result = notify_mod.notify(
                subject, markdown, render.render_html(markdown, today),
                settings.notify.get("channel", "email"),
            )
            sent = result.ok
        except notify_mod.NotifyError as e:
            err(f"推送失败：{e}")
            _archive(today, digest, candidates, collected, stats, settings, grounding, backend, sent=False)
            return 1

    _archive(today, digest, candidates, collected, stats, settings, grounding, backend, sent)

    if not args.dry_run and not args.ignore_seen:
        _write_state(today, state, candidates, upgrades, settings, sent, now)

    if not grounding.ok:
        return 1
    if will_push and not sent and not is_empty:
        err("报告已生成但未送达——按「送不出去 = 失败」返回 1")
        return 1

    ok(f"完成：{today}（{len(digest.items)} 条，backend={backend}，sent={sent}）")
    return 0


def _archive(today, digest, candidates, collected, stats, settings, grounding, backend, sent) -> None:
    payload = render.build_report_json(
        today, digest, candidates, collected.reports, stats, settings,
        grounding=grounding, backend=backend, sent=sent,
    )
    save_history(today, payload)


def _write_state(today, state, candidates, upgrades, settings, sent, now) -> None:
    mark_seen([c.record for c in candidates], state, today)
    # 版本升级上报之后必须改记录，否则下周会再报一次
    for paper in upgrades:
        if paper.work_id:
            state.mark_upgraded(paper.work_id, "journal")
    state.prune(today, settings.prune_days)
    state.last_report_date = today
    state.sent = sent
    if sent:
        state.last_success_at = now.isoformat()
    save_state(state)
    log("状态", f"已见 {len(state.seen)} 条（本次新增 {state.newly_marked}）")


def _repush(today, settings, state, args) -> int:
    """报告文件已存在时，只重试推送。"""
    from scidaily.settings import REPORTS_DIR

    path = REPORTS_DIR / f"{today}.md"
    if not path.exists():
        warn("history 有记录但 .md 不存在，按重新生成处理")
        return cmd_run(argparse.Namespace(**{**vars(args), "force": True}))

    markdown = path.read_text(encoding="utf-8")
    if args.dry_run:
        info("--dry-run，不推送")
        return 0
    try:
        subject = f"{settings.notify.get('subject_prefix', '[科学前沿]')} {today}"
        notify_mod.notify(
            subject, markdown, render.render_html(markdown, today),
            settings.notify.get("channel", "email"),
        )
    except notify_mod.NotifyError as e:
        err(f"重试推送仍失败：{e}")
        return 1
    state.sent = True
    from .settings import now_local as _nl

    state.last_success_at = _nl(settings).isoformat()
    save_state(state)
    ok("重试推送成功")
    return 0


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------


def cmd_alert(args) -> int:
    try:
        settings = load_settings()
        notify_mod.notify(
            "[科学前沿] 运行失败", args.alert, render.render_html(args.alert, "alert"),
            settings.notify.get("channel", "email"),
        )
        ok("告警已发送")
    except Exception as e:  # noqa: BLE001 - 告警失败不能加剧已经失败的运行
        warn(f"告警发送失败（不影响退出码）：{e}")
    return 0


def cmd_check_sources(args) -> int:
    """逐个源体检：哪个 API 挂了、哪个期刊 0 条。"""
    load_env()
    settings = load_settings()
    banner("数据源体检")
    collected = collect(settings, offline=False, ttl_hours=0.0, limit=args.limit)

    print()
    print(f"{'源':<28} {'状态':<14} {'抓到':>6} {'留存':>6} {'耗时':>8}")
    print("-" * 70)
    for r in collected.reports:
        flag = "  " if r.ok else "!!"
        print(f"{flag}{r.name[:26]:<26} {r.status:<14} {r.fetched:>6} {r.kept:>6} {r.elapsed_s:>7.1f}s")
        if r.error:
            print(f"    └─ {r.error[:100]}")
    print()
    bad = [r for r in collected.reports if not r.ok]
    if bad:
        warn(f"{len(bad)}/{len(collected.reports)} 个源失败")
        return 1
    ok("所有源正常")
    return 0


def cmd_seed(args) -> int:
    """抓一次并固化到 tests/fixtures，之后可零网络开发。"""
    load_env()
    settings = load_settings()
    banner("固化离线固件")
    ttl = 0.0 if getattr(args, "fresh", False) else cache.TTL_HOURS_LOCAL
    collect(settings, offline=False, ttl_hours=ttl)
    written = cache.seed_fixtures()
    for p in written:
        info(f"  {p.name}")
    ok(f"已固化 {len(written)} 个响应到 tests/fixtures/api/")
    return 0


def cmd_selftest(args) -> int:
    """发一封测试邮件。不要跳过这一步。"""
    try:
        result = notify_mod.selftest(args.channel)
    except notify_mod.NotifyError as e:
        err(str(e))
        return 1
    ok(f"自检邮件已发送（{result.detail or result.channel}）")
    return 0


def cmd_weekly(args) -> int:
    """每周顶刊盘点：慢刊回看。

    与日报的关键差异：它**不参与 seen 去重**（这是回顾，不是新条目），
    改用 last_weekly_date 防止同一周重复出。
    """
    load_env()
    settings = load_settings()
    state = load_state()
    today = report_date(settings)

    if state.last_weekly_date == today and not getattr(args, "force", False):
        ok("本周盘点已生成，跳过")
        return 0

    banner("每周顶刊盘点")
    now = now_local(settings)
    offline = bool(getattr(args, "offline", False))

    # 复用日报的采集路径（含 PubMed 的 PublicationType 过滤），只是窗口更长。
    # 回看窗口：先试 14 天，一无所获就放宽到 90 天——
    # JMLR 不定期、Acta Numerica 一年一卷，小窗口对它们永远是空的。
    def _grab(days: int):
        return collect(
            settings, offline=offline, ttl_hours=0.0, window_hours=days * 24
        )

    collected = _grab(settings.weekly_window_days)
    if not collected.papers:
        warn(
            f"{settings.weekly_window_days} 天内无新条目，放宽到 "
            f"{settings.weekly_fallback_days} 天再试"
        )
        collected = _grab(settings.weekly_fallback_days)

    merged, _ = merge.dedupe_in_run(list(collected.papers))

    # ★ 周度盘点也要过惩罚词过滤。
    #   实测漏进来过 JAMA 的 "Editors' Summary Podcast" 和 "Holding Hope"（人文小品）。
    #   日报通道本来就会过滤掉它们，盘点若不过滤就会变成一张噪音表。
    from .filter import _is_penalised

    merged = [p for p in merged if not _is_penalised(p, settings)]

    ranked = velocity.rank_hot(merged, settings, now=now)
    top = ranked[:20]

    lines = [f"# 顶刊盘点 · 近 {settings.weekly_window_days} 天", ""]
    if not merged:
        lines.append("本期无新条目。")
    else:
        by_disc: dict[str, list] = {}
        for p in merged:
            by_disc.setdefault(p.discipline, []).append(p)
        for disc, group in sorted(by_disc.items()):
            lines.append(f"## {disc}（{len(group)} 篇）")
            lines.append("")
            for p in sorted(group, key=lambda x: -x.citation_count)[:8]:
                lines.append(f"- **{p.title}** — {p.journal} `{p.tier}`")
                if p.doi:
                    lines.append(f"  <sub>doi:{p.doi}</sub>")
            lines.append("")
        if top:
            lines.append("## 近期被引最多")
            lines.append("")
            for p, sc in top[:5]:
                lines.append(f"- {p.title} — {sc.explain()}")

    markdown = "\n".join(lines)
    from scidaily.settings import REPORTS_DIR

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"weekly-{today}.md"
    out.write_text(markdown, encoding="utf-8")
    ok(f"盘点已写入 {out.name}（{len(merged)} 篇）")

    if not getattr(args, "dry_run", False):
        state.last_weekly_date = today
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
