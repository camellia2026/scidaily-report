"""分发层：把日报推给用户。

## 为什么推送的是「摘要 + 链接」而不只是正文

原文档设想的是「邮件推送完整日报」。邮件可以承载全文，但手机端
Markdown 渲染很糟——所以邮件用 multipart/alternative 发两份：

    text/plain   纯 Markdown 原文（存档 / 纯文本客户端）
    text/html    内联样式的 HTML（手机上看这个）

★ 刻意不复用学习区的 build_html.py：那是生成带侧边栏的整站阅读版，
  对邮件完全跑偏（外部 CSS 在多数邮件客户端里会被直接剥离）。

## 先证明，再信任

`--selftest` 发一封测试邮件。这个能力做成 workflow_dispatch 的一个输入，
让你在**信任这条渠道之前**先验证它通不通——
而不是等到第一个早上才发现授权码写错了。
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from common.console import log

from .settings import MailConfig, get_mail_config


class NotifyError(Exception):
    """推送失败。message 里带修复指引。"""


@dataclass
class NotifyResult:
    channel: str
    ok: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------


def _connect(mail: MailConfig):
    """按端口选择连接方式。

    465 = 隐式 TLS（SMTP_SSL）。QQ / 163 用这个。
    587 = 显式 TLS（STARTTLS）。部分环境 465 被挡时的备选。

    ★ 云环境里 25 端口是明确被封的，永远不要用 25。
    """
    ctx = ssl.create_default_context()
    if mail.port == 465:
        return smtplib.SMTP_SSL(mail.host, mail.port, timeout=30, context=ctx)
    conn = smtplib.SMTP(mail.host, mail.port, timeout=30)
    conn.ehlo()
    conn.starttls(context=ctx)
    conn.ehlo()
    return conn


def send_email(
    subject: str,
    markdown: str,
    html: str,
    *,
    mail: MailConfig | None = None,
) -> NotifyResult:
    """发送 multipart/alternative 邮件。"""
    mail = mail or get_mail_config()

    missing = mail.missing
    if missing:
        raise NotifyError(
            f"邮件配置不完整，缺少：{', '.join(missing)}\n"
            f"提示：QQ/163 邮箱需要在「设置 → 账户」里开启 SMTP 服务并"
            f"生成**授权码**（不是登录密码），填进 MAIL_AUTH_CODE。\n"
            f"  本地：写进 .env\n"
            f"  CI：写进 GitHub Secrets"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    # From 必须等于认证用的账号，否则多数服务端直接 550
    msg["From"] = formataddr(("科学前沿", mail.username))
    msg["To"] = mail.to
    msg["Date"] = formatdate(localtime=True)

    msg.set_content(markdown, subtype="plain", charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")

    # ★ 分三段做，因为「在哪个阶段断的」决定了真正的原因——
    #   而 QQ/163 在凭据错误时是**直接断开连接**，不返回认证失败。
    #   如果不分段，一次「授权码填错了」会被报成「端口不通」，
    #   把人引到完全错误的方向去排查。（这是实测踩到的。）

    # 第 1 段：连接
    try:
        conn = _connect(mail)
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(
            f"连不上 {mail.host}:{mail.port}（{type(e).__name__}: {e}）\n"
            f"提示：465 走隐式 TLS，587 走 STARTTLS。\n"
            f"  云环境里 25 端口是明确被封的，别用。\n"
            f"  如果是 465 连不上，把 MAIL_SMTP_PORT 改成 587 再试。"
        ) from e

    # 第 2 段：登录
    try:
        conn.login(mail.username, mail.auth_code)
    except smtplib.SMTPAuthenticationError as e:
        conn.close()
        raise NotifyError(
            f"SMTP 认证被拒（{e.smtp_code}）。\n"
            f"最常见的原因：把**登录密码**填成了授权码 —— QQ/163 必须用授权码。\n"
            f"另一个可能：From 地址（{mail.username}）和认证账号不一致。"
        ) from e
    except (smtplib.SMTPException, OSError) as e:
        conn.close()
        # 实测：授权码错误时 QQ 就是走到这里，而不是 SMTPAuthenticationError
        raise NotifyError(
            f"登录阶段连接被服务端断开（{type(e).__name__}: {e}）。\n"
            f"★ QQ/163 在凭据错误时的表现就是这样 —— 它不报「认证失败」，直接断连接。\n"
            f"  所以这几乎可以确定是**授权码不对**（不是端口问题）。\n"
            f"  检查 MAIL_AUTH_CODE：必须是邮箱设置里生成的 16 位授权码，\n"
            f"  不是登录密码；确认没有多余空格或引号。\n"
            f"  账号：{mail.username}"
        ) from e

    # 第 3 段：发送
    try:
        conn.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(
            f"登录成功但发送失败（{type(e).__name__}: {e}）\n"
            f"提示：多半是收件地址（{mail.to}）有问题，"
            f"或发件人与认证账号不一致导致被拒。"
        ) from e
    finally:
        try:
            conn.quit()
        except Exception:  # noqa: BLE001 — 退出失败不影响「已发送」这个事实
            pass

    log("通知", f"邮件已发送至 {mail.to}")
    return NotifyResult(channel="email", ok=True, detail=mail.to)


# ---------------------------------------------------------------------------
# 控制台（本地调试）
# ---------------------------------------------------------------------------


def send_console(subject: str, markdown: str, **_) -> NotifyResult:
    print()
    print("=" * 70)
    print(f"[控制台推送] {subject}")
    print("=" * 70)
    preview = markdown if len(markdown) < 4000 else markdown[:4000] + "\n...（已截断）"
    print(preview)
    print("=" * 70)
    print()
    return NotifyResult(channel="console", ok=True)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


def notify(
    subject: str,
    markdown: str,
    html: str,
    channel: str,
    *,
    mail: MailConfig | None = None,
) -> NotifyResult:
    channel = (channel or "console").strip().lower()
    if channel == "email":
        return send_email(subject, markdown, html, mail=mail)
    if channel == "console":
        return send_console(subject, markdown)
    raise NotifyError(f"未知的推送渠道 {channel!r}（可选：email / console）")


def selftest(channel: str | None = None) -> NotifyResult:
    """发一封测试邮件，用来在信任这条渠道之前先验证它。

    在 Actions 里通过 workflow_dispatch 的 selftest 输入触发。
    """
    from .settings import load_settings

    try:
        channel = channel or load_settings().notify.get("channel", "email")
    except Exception:  # noqa: BLE001
        channel = channel or "email"

    import os

    from .settings import report_date

    try:
        date = report_date(load_settings())
    except Exception:  # noqa: BLE001
        from datetime import datetime
        date = datetime.now().strftime("%Y-%m-%d")

    body = (
        f"# 渠道自检 · {date}\n"
        "\n"
        "如果你在手机上看到这封邮件，说明 SMTP 渠道是通的。\n"
        "\n"
        "## 检查项\n"
        "\n"
        "| 项目 | 状态 |\n"
        "|---|---|\n"
        "| SMTP 连接 | OK |\n"
        "| 认证 | OK |\n"
        "| HTML 渲染 | 如果你看到的是排版过的卡片而不是纯文本，说明这一项也 OK |\n"
        "\n"
        "---\n"
        "\n"
        "*这是 scidaily-report 的渠道自检邮件，可以忽略。*\n"
    )
    from .render import render_html

    return notify(
        f"[科学前沿] 渠道自检 {date}",
        body,
        render_html(body, date),
        channel,
    )
