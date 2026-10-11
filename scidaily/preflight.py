"""启动自检：在真正开跑之前，把「配置为空」这类问题当场揪出来。

## 为什么需要这个

2026-09-25 的第一次真实运行暴露了这类问题：4 个 GitHub Secrets 被误建成了
Variables，于是 `secrets.*` 全部读不到。表现是：

    [WARN] 后端 deepseek 不可用，跳过：使用 deepseek 需要 DEEPSEEK_API_KEY
    [WARN] 后端 github 调用失败（fatal）：AttributeError: ...
    [WARN] LLM 调用失败：所有 LLM 后端都失败了
    [ERR]  推送失败：邮件配置不完整，缺少：MAIL_USERNAME, MAIL_AUTH_CODE
    [OK]   完成：2026-09-25 日报已生成（未推送，backend=rule-fallback）

一个**绿色的运行**，跑完全程，什么都没送出去。用户看到的只有绿勾。

如果当时有这道自检，第一次运行的第 2 秒就会直接说：
「MAIL_USERNAME 为空 —— 你可能建在了 Variables 而不是 Secrets」。

## 设计原则

错误信息必须**指向最可能的原因和修法**，而不是说「缺少配置」。
配置错误是运维里最高频、也最不值得让人猜的一类故障。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from common.console import warn

from .settings import load_env


class PreflightError(Exception):
    """启动自检未通过。message 里带具体的修法。"""


# 每个后端需要哪个环境变量
_PROVIDER_KEYS = {
    "deepseek": ["DEEPSEEK_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "github": ["GITHUB_MODELS_TOKEN"],
    "ollama": [],   # 本地模型，不需要 key
    "mock": [],     # 离线假模型
}

# 从没配置过、或配置得不对时的通用提示
_SECRET_HINT = (
    "  · 本地：写进项目根目录的 .env\n"
    "  · CI：写进 GitHub 仓库的 Secrets（Settings → Secrets and variables → Actions）\n"
    "  ⚠️ 最常见的错法：把该建在 **Secrets** 标签页的项建到了 **Variables** 标签页。\n"
    "     两个标签页紧挨着、长得几乎一样，但 workflow 里 secrets.X 读不到 vars 里的东西。\n"
    "     自查方法：打开任意一次运行的日志，展开环境变量那段，\n"
    "     如果这一项显示为空，就说明它没被当成 Secret 注入。"
)


@dataclass
class PreflightReport:
    problems: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def _get(name: str) -> str:
    return (os.getenv(name) or "").strip()


def check(
    *,
    provider: str | None,
    fallback: str | None = None,
    channel: str = "email",
    will_push: bool = True,
    use_llm: bool = True,
) -> PreflightReport:
    """检查运行需要的配置是否齐备。

    Args:
        provider: 主 LLM 后端名
        fallback: 兜底后端名（可为 None）
        channel: 推送渠道
        will_push: 本次是否会推送（--dry-run 时为 False，此时不要求邮件配置）
        use_llm: 本次是否真的会调模型（--no-llm 时为 False）
    """
    load_env()
    problems: list[str] = []
    warnings: list[str] = []

    # ---------- LLM ----------
    if use_llm:
        names = [n for n in (provider, fallback) if n]
        usable: list[str] = []
        missing: dict[str, list[str]] = {}

        for name in dict.fromkeys(names):   # 去重且保序
            need = _PROVIDER_KEYS.get(name)
            if need is None:
                warnings.append(f"未知的 LLM 后端 {name!r}，无法预检（可用：{', '.join(_PROVIDER_KEYS)}）")
                continue
            absent = [k for k in need if not _get(k)]
            if absent:
                missing[name] = absent
            else:
                usable.append(name)

        if not usable:
            lines = ["没有任何一个可用的 LLM 后端："]
            for name, absent in missing.items():
                lines.append(f"  · {name} 缺少 {', '.join(absent)}")
            if not missing:
                lines.append("  · （没有配置任何后端）")
            lines.append("")
            lines.append("怎么修：")
            lines.append(f"  · 填上上面列出的变量，或把 LLM_PROVIDER 改成 mock（离线假模型）")
            lines.append("")
            lines.append(_SECRET_HINT)
            problems.append("\n".join(lines))
        elif missing:
            # 主后端挂了但有兜底 —— 能跑，但要说清楚
            for name, absent in missing.items():
                warnings.append(
                    f"后端 {name} 缺少 {', '.join(absent)}，将被跳过"
                    f"（剩下 {', '.join(usable)} 可用）"
                )

    # ---------- 推送 ----------
    if will_push and channel == "email":
        absent = [k for k in ("MAIL_USERNAME", "MAIL_AUTH_CODE") if not _get(k)]
        if absent:
            problems.append(
                f"推送渠道是邮件，但缺少：{', '.join(absent)}\n"
                "\n"
                "怎么修：\n"
                "  · MAIL_USERNAME = 完整邮箱地址（必须与发件人一致，否则 550）\n"
                "  · MAIL_AUTH_CODE = SMTP 授权码，**不是登录密码**\n"
                "    QQ邮箱：设置 → 账户 → 开启 POP3/SMTP 服务 → 生成授权码（16 位）\n"
                "\n"
                + _SECRET_HINT
            )

    return PreflightReport(problems=problems, warnings=warnings)


def run_or_raise(**kwargs) -> PreflightReport:
    """跑一次自检；不通过就抛 PreflightError。"""
    report = check(**kwargs)
    for w in report.warnings:
        warn(w)
    if not report.ok:
        raise PreflightError("\n\n".join(report.problems))
    return report
