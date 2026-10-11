"""把当天运行的结果渲染成 GitHub Actions 的「运行摘要」。

为什么单独做成模块而不是写在 workflow 的 run: 里：

1. YAML 块标量里嵌 Python heredoc 对缩进极其敏感，改一次错一次
2. 这段逻辑值得被测试，而写在 YAML 里就测不了
3. 没有 gh CLI 的时候，run 页面上的这段摘要是你在手机上
   唯一能看到的现场——它太重要了，不该是一段没人敢动的脚本

用法（workflow 里）：
    python -m scidaily.summary >> "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import json
import sys

from .settings import load_env, load_settings, report_date
from .state import history_path

_OK_STATUSES = ("ok", "cached", "empty")


def render_summary(date: str | None = None) -> str:
    """生成摘要 Markdown。没有当日数据时也返回一段说明，而不是空字符串。"""
    load_env()

    try:
        settings = load_settings()
        date = date or report_date(settings)
    except Exception:  # noqa: BLE001 — 摘要本身绝不能成为失败原因
        from datetime import datetime, timedelta, timezone

        date = date or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    path = history_path(date)
    if not path.exists():
        return (
            f"### 运行摘要 · {date}\n\n"
            "未找到当日 `data/history/*.json`——运行很可能在生成报告之前就失败了。\n"
            "往上翻看是哪一步报的错。\n"
        )

    try:
        j = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return f"### 运行摘要 · {date}\n\nhistory 文件读取失败：{e}\n"

    sources = j.get("sources") or []
    good = [s for s in sources if s.get("status") in _OK_STATUSES]
    bad = [s for s in sources if s.get("status") not in _OK_STATUSES]
    grounding = j.get("grounding") or {}
    stats = j.get("stats") or {}

    lines = [f"### 运行摘要 · {j.get('date', date)}", ""]
    lines.append(f"- **后端**：`{j.get('backend', '?')}`")

    items = j.get("items") or []
    by_channel: dict[str, int] = {}
    for it in items:
        for rec in it.get("records") or [{}]:
            channel = rec.get("channel") or "?"
            by_channel[channel] = by_channel.get(channel, 0) + 1
            break
    breakdown = " / ".join(f"{n} {ch}" for ch, n in sorted(by_channel.items()))
    lines.append(f"- **条目**：{len(items)} 条" + (f"（{breakdown}）" if breakdown else ""))

    lines.append(f"- **已推送**：{'是' if j.get('sent') else '否'}")
    lines.append(f"- **反幻觉校验**：{'通过' if grounding.get('ok') else '未通过'}")
    for prob in grounding.get("problems") or []:
        lines.append(f"  - ⚠️ {prob}")

    lines.append(f"- **数据源**：{len(good)}/{len(sources)} 正常")
    for s in bad:
        lines.append(f"  - ❌ {s.get('name')}：{s.get('status')} {s.get('error') or ''}".rstrip())

    # stats 现在是「每通道一份」的字典，不是原来的扁平结构
    for channel, st in sorted(stats.items()):
        if not isinstance(st, dict) or not st.get("fetched"):
            continue
        lines.append(
            f"  - `{channel}`：抓取 {st.get('fetched', '?')} → "
            f"历史已见 {st.get('already_seen', '?')} → "
            f"送模型 {st.get('candidates', '?')}"
        )

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sys.stdout.write(render_summary(argv[0] if argv else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
