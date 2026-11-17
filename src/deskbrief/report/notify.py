"""Email delivery and the morning report hooks (spec §5.5, §9, §11).

``Notifier`` is the one seam to the outside world: ``SmtpNotifier`` sends through SMTP using
settings from the environment (spec §11: ``SMTP_USER``, ``SMTP_APP_PASSWORD`` and the recipients
variable named in ``config.report.recipients_env``; ``SMTP_HOST``/``SMTP_PORT`` are optional and
default to Gmail over implicit TLS). ``NullNotifier`` sends nothing (dry runs, local runs). The
password is never logged or put in an error: it is kept out of ``repr`` and scrubbed from every
error text with the repo's redaction.

``report_steps`` builds the two morning hooks the runner calls after the checks:

* *build*: ``BriefModel`` -> HTML + ``.xlsx`` + ``model.json``, archived to
  ``reports/date={d}/v{n}/`` (spec §6; ``n`` is one more than the highest archived version, so a
  re-run never overwrites). Returns the ``brief.html`` key for the ``runs`` row.
* *notify*: emails what *this run* built, with the workbook attached. Archiving comes first, so an
  email failure leaves the report archived. The failure is logged at ``error`` (redacted, event
  ``report.email_failed``) and re-raised: the runner records it and ends the run ``degraded``.

The hooks share one closure, so ``notify`` can only send a report that ``build`` produced in the
same run (never a stale one from an earlier run). The runner gives hooks only the ``RunContext``.
"""

from __future__ import annotations

import os
import re
import smtplib
import ssl
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol, runtime_checkable

from deskbrief.logging import get_logger, redact, redact_text
from deskbrief.models import RunContext, Trigger
from deskbrief.report.excel import write_workbook
from deskbrief.report.html import render_html, render_text
from deskbrief.report.model import BriefModel
from deskbrief.store.backend import REPORTS_PREFIX, StorageBackend, report_key
from deskbrief.store.duck import connect

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_TIMEOUT_S = 30


class NotifyError(Exception):
    """Delivery failed. The text is already redacted."""


class NotifyConfigError(NotifyError):
    """SMTP settings are missing or invalid (names variables, never values)."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes
    mime_type: str


@dataclass(frozen=True)
class SentMessage:
    subject: str
    html: str
    text: str
    attachments: tuple[Attachment, ...] = ()


@runtime_checkable
class Notifier(Protocol):
    def send(
        self, subject: str, html: str, text: str, attachments: Sequence[Attachment] = ()
    ) -> None: ...


@dataclass
class NullNotifier:
    """Sends nothing; remembers what it was given (dry runs, local runs, tests)."""

    messages: list[SentMessage] = field(default_factory=list)

    def send(
        self, subject: str, html: str, text: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        self.messages.append(SentMessage(subject, html, text, tuple(attachments)))
        get_logger().info("report.email_not_sent", reason="null notifier", subject=subject)


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    sender: str
    recipients: tuple[str, ...]

    @classmethod
    def from_env(
        cls, env: Mapping[str, str], recipients_env: str = "REPORT_RECIPIENTS"
    ) -> SmtpSettings:
        missing = [n for n in ("SMTP_USER", "SMTP_APP_PASSWORD", recipients_env) if not env.get(n)]
        if missing:
            raise NotifyConfigError("missing SMTP settings: " + ", ".join(missing))
        recipients = tuple(r for r in re.split(r"[,;\s]+", env[recipients_env]) if r)
        if not recipients:
            raise NotifyConfigError(f"{recipients_env} lists no recipients")
        try:
            port = int(env.get("SMTP_PORT") or DEFAULT_SMTP_PORT)
        except ValueError:
            raise NotifyConfigError("SMTP_PORT is not a number") from None
        return cls(
            host=env.get("SMTP_HOST") or DEFAULT_SMTP_HOST,
            port=port,
            user=env["SMTP_USER"],
            password=env["SMTP_APP_PASSWORD"],
            sender=env["SMTP_USER"],
            recipients=recipients,
        )


def _connect(s: SmtpSettings) -> smtplib.SMTP:
    """Implicit TLS on 465, STARTTLS otherwise; certificates are verified."""
    context = ssl.create_default_context()
    if s.port == DEFAULT_SMTP_PORT:
        return smtplib.SMTP_SSL(s.host, s.port, timeout=_TIMEOUT_S, context=context)
    smtp = smtplib.SMTP(s.host, s.port, timeout=_TIMEOUT_S)
    try:
        smtp.starttls(context=context)
    except Exception:
        smtp.close()
        raise
    return smtp


class SmtpNotifier:
    """Sends a multipart (text + HTML) message with optional attachments over SMTP."""

    def __init__(
        self,
        settings: SmtpSettings,
        connect: Callable[[SmtpSettings], smtplib.SMTP] = _connect,
    ) -> None:
        self._s = settings
        self._connect = connect

    def send(
        self, subject: str, html: str, text: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        s = self._s
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, s.sender, ", ".join(s.recipients)
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        for a in attachments:
            maintype, _, subtype = a.mime_type.partition("/")
            msg.add_attachment(a.data, maintype=maintype, subtype=subtype, filename=a.filename)
        try:
            with self._connect(s) as smtp:
                smtp.login(s.user, s.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            detail = redact_text(f"{type(exc).__name__}: {exc}", [s.password])
            raise NotifyError(f"SMTP send failed: {detail}") from None


def notifier_for(ctx: RunContext, env: Mapping[str, str]) -> Notifier:
    """SMTP when configured. Nothing configured: a manual or local run is a logged null send; a
    scheduled run is an error (a cron run that cannot email must not look fine)."""
    names = ("SMTP_USER", "SMTP_APP_PASSWORD", ctx.config.report.recipients_env)
    if ctx.trigger is not Trigger.SCHEDULE and not any(env.get(n) for n in names):
        return NullNotifier()
    return SmtpNotifier(SmtpSettings.from_env(env, ctx.config.report.recipients_env))


# ---------------------------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------------------------


def archive_report(store: StorageBackend, report_date: date, files: Mapping[str, bytes]) -> str:
    """Write ``files`` to the next free ``reports/date={d}/v{n}/``; returns ``brief.html``'s key."""
    prefix = f"{REPORTS_PREFIX}/date={report_date.isoformat()}/"
    seen = [int(m[1]) for k in store.list(prefix) if (m := re.match(r"v(\d+)/", k[len(prefix) :]))]
    version = max(seen, default=0) + 1
    for name, data in files.items():
        store.put_bytes(report_key(report_date, version, name), data)
    return report_key(report_date, version, "brief.html")


def _workbook_bytes(model: BriefModel) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        return write_workbook(model, Path(tmp) / "brief.xlsx").read_bytes()


# ---------------------------------------------------------------------------------------------
# the morning hooks
# ---------------------------------------------------------------------------------------------


@dataclass
class _Built:
    subject: str
    html: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)


def report_steps(
    notifier: Notifier | None = None,
) -> tuple[Callable[[RunContext], str | None], Callable[[RunContext], None]]:
    """``(build_report, notify)`` hooks for one run; ``notifier`` defaults to ``notifier_for``."""
    built: list[_Built] = []

    def build(ctx: RunContext) -> str | None:
        duck = connect(ctx.store, ctx.config)
        try:
            model = BriefModel.from_store(duck, ctx.config, ctx.target_date, ctx.now_utc)
        finally:
            duck.close()
        html = render_html(model)
        mail = _Built(f"{model.title} · {model.health.text}", html, render_text(model))
        built.append(mail)  # from here on the email can go out even if archiving fails
        workbook = _workbook_bytes(model)
        name = f"brief-{ctx.target_date.isoformat()}.xlsx"
        mail.attachments.append(Attachment(name, workbook, XLSX_MIME))
        key = archive_report(
            ctx.store,
            ctx.target_date,
            {
                "brief.html": html.encode(),
                "brief.xlsx": workbook,
                "model.json": model.to_json().encode(),
            },
        )
        get_logger(run_id=ctx.run_id).info("report.archived", key=key)
        return key

    def notify(ctx: RunContext) -> None:
        log = get_logger(run_id=ctx.run_id)
        if not built:
            log.warning("report.email_skipped", reason="no report was built in this run")
            return
        mail = built[-1]
        try:
            sender = notifier or notifier_for(ctx, os.environ)
            sender.send(mail.subject, mail.html, mail.text, mail.attachments)
        except Exception as exc:  # logged as the alert, then re-raised so the run is degraded
            log.error("report.email_failed", error=redact(f"{type(exc).__name__}: {exc}"))
            raise

    return build, notify


__all__ = [
    "Attachment",
    "Notifier",
    "NotifyConfigError",
    "NotifyError",
    "NullNotifier",
    "SentMessage",
    "SmtpNotifier",
    "SmtpSettings",
    "archive_report",
    "notifier_for",
    "report_steps",
]
