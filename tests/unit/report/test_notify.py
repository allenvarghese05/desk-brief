"""Notifier protocol: SmtpNotifier (mocked SMTP, no network), NullNotifier, settings from env.

SYNTHETIC FIXTURE: addresses use the reserved ``.test`` domain and the password is made up.
INJECTED FAULT: the SMTP fakes below raise errors whose text contains the password.
"""

from __future__ import annotations

import base64
import smtplib
import threading
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from deskbrief.report.notify import (
    Attachment,
    Notifier,
    NotifyConfigError,
    NotifyError,
    NullNotifier,
    SmtpNotifier,
    SmtpSettings,
    archive_report,
)
from deskbrief.store.backend import KeyExistsError
from deskbrief.store.local import LocalBackend

PASSWORD = "hunter2-synthetic-app-password"
ENV = {
    "SMTP_USER": "sender@mail.test",
    "SMTP_APP_PASSWORD": PASSWORD,
    "REPORT_RECIPIENTS": "a@mail.test, b@mail.test",
}


class FakeSmtp:
    """Stands in for ``smtplib.SMTP``: records calls, optionally raises."""

    def __init__(self, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.logins: list[tuple[str, str]] = []
        self.sent: list[EmailMessage] = []

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        if self.fail_at == "login":
            raise smtplib.SMTPAuthenticationError(535, f"bad credentials {password}".encode())
        self.logins.append((user, password))

    def send_message(self, msg: EmailMessage) -> dict[str, Any]:
        if self.fail_at == "value":
            raise ValueError(f"header rejected for {self.logins[-1][1]}")
        if self.fail_at == "b64":
            plain = base64.b64encode(f"\0sender@mail.test\0{PASSWORD}".encode()).decode()
            raise smtplib.SMTPException(f"C: AUTH PLAIN {plain}")
        if self.fail_at == "b64_alone":
            alone = base64.b64encode(PASSWORD.encode()).decode()
            raise smtplib.SMTPException(f"C: {alone}")
        if self.fail_at == "send":
            raise smtplib.SMTPServerDisconnected(f"connection lost after login of {PASSWORD}")
        self.sent.append(msg)
        return {}


def notifier(smtp: FakeSmtp) -> SmtpNotifier:
    return SmtpNotifier(SmtpSettings.from_env(ENV), connect=lambda settings: smtp)


def test_settings_come_from_env_and_hide_the_password() -> None:
    s = SmtpSettings.from_env(ENV)
    assert (s.user, s.sender, s.host, s.port) == (
        "sender@mail.test",
        "sender@mail.test",
        "smtp.gmail.com",
        465,
    )
    assert s.recipients == ("a@mail.test", "b@mail.test")
    assert PASSWORD not in repr(s)
    assert PASSWORD not in str(s)
    s2 = SmtpSettings.from_env({**ENV, "SMTP_HOST": "smtp.mail.test", "SMTP_PORT": "587"})
    assert (s2.host, s2.port) == ("smtp.mail.test", 587)


def test_recipients_variable_name_is_configurable() -> None:
    env = {k: v for k, v in ENV.items() if k != "REPORT_RECIPIENTS"} | {"MY_LIST": "c@mail.test"}
    assert SmtpSettings.from_env(env, recipients_env="MY_LIST").recipients == ("c@mail.test",)


def test_missing_settings_name_the_variables_not_their_values() -> None:
    with pytest.raises(NotifyConfigError) as err:
        SmtpSettings.from_env({"SMTP_USER": "sender@mail.test"})
    assert "SMTP_APP_PASSWORD" in str(err.value)
    assert "REPORT_RECIPIENTS" in str(err.value)
    assert "sender@mail.test" not in str(err.value)
    with pytest.raises(NotifyConfigError):
        SmtpSettings.from_env({**ENV, "SMTP_PORT": "not-a-port"})
    with pytest.raises(NotifyConfigError):
        SmtpSettings.from_env({**ENV, "REPORT_RECIPIENTS": " , "})


def test_send_builds_a_multipart_message_and_logs_in() -> None:
    smtp = FakeSmtp()
    attachment = Attachment("brief.xlsx", b"PK-synthetic", "application/vnd.test-sheet")
    notifier(smtp).send("Subject line", "<p>hi</p>", "hi", [attachment])
    assert smtp.logins == [("sender@mail.test", PASSWORD)]
    (msg,) = smtp.sent
    assert msg["Subject"] == "Subject line"
    assert msg["From"] == "sender@mail.test"
    assert msg["To"] == "a@mail.test, b@mail.test"
    assert msg.get_body(preferencelist=("plain",)).get_content().strip() == "hi"  # type: ignore[union-attr]
    assert msg.get_body(preferencelist=("html",)).get_content().strip() == "<p>hi</p>"  # type: ignore[union-attr]
    (part,) = list(msg.iter_attachments())
    assert part.get_filename() == "brief.xlsx"
    assert part.get_content() == b"PK-synthetic"


@pytest.mark.parametrize("fail_at", ["login", "send"])
def test_smtp_failure_raises_notify_error_without_the_password(fail_at: str) -> None:
    with pytest.raises(NotifyError) as err:
        notifier(FakeSmtp(fail_at)).send("s", "<p>x</p>", "x")
    assert PASSWORD not in str(err.value)
    assert type(err.value) is NotifyError
    assert "SMTP" in str(err.value)


def test_connection_refused_is_a_notify_error() -> None:
    def refuse(settings: SmtpSettings) -> FakeSmtp:
        raise ConnectionRefusedError(f"cannot reach {settings.host}")

    with pytest.raises(NotifyError, match="ConnectionRefusedError"):
        SmtpNotifier(SmtpSettings.from_env(ENV), connect=refuse).send("s", "h", "t")


def test_null_notifier_sends_nothing_and_remembers_the_message() -> None:
    null = NullNotifier()
    null.send("subject", "<p>x</p>", "x", [Attachment("a.bin", b"1", "application/octet-stream")])
    assert [m.subject for m in null.messages] == ["subject"]
    assert null.messages[0].attachments[0].filename == "a.bin"


def test_both_notifiers_satisfy_the_protocol() -> None:
    assert isinstance(NullNotifier(), Notifier)
    assert isinstance(notifier(FakeSmtp()), Notifier)


def test_non_smtp_exceptions_are_redacted_into_a_notify_error() -> None:
    """A ``ValueError`` (say, a rejected header) used to escape ``send`` with the password in it."""
    with pytest.raises(NotifyError) as err:
        notifier(FakeSmtp("value")).send("s", "<p>x</p>", "x")
    assert PASSWORD not in str(err.value)
    assert "ValueError" in str(err.value)
    assert err.value.__cause__ is None
    assert err.value.__suppress_context__


@pytest.mark.parametrize("fail_at", ["b64", "b64_alone"])
def test_base64_forms_of_the_credentials_are_masked(fail_at: str) -> None:
    forms = [
        base64.b64encode(f"\0sender@mail.test\0{PASSWORD}".encode()).decode(),
        base64.b64encode(PASSWORD.encode()).decode(),
    ]
    with pytest.raises(NotifyError) as err:
        notifier(FakeSmtp(fail_at)).send("s", "<p>x</p>", "x")
    for leaked in (PASSWORD, *forms, *(f.rstrip("=") for f in forms)):
        assert leaked not in str(err.value)
    assert "SMTPException" in str(err.value)


# ---------------------------------------------------------------------------------------------
# archive_report
# ---------------------------------------------------------------------------------------------

DAY = date(2026, 11, 16)


class RacingStore(LocalBackend):
    """Both archivers list before either writes (a barrier in ``list``): the real race."""

    def __init__(self, root: Path, parties: int) -> None:
        super().__init__(root)
        self._gate = threading.Barrier(parties, timeout=10)
        self._first_listing = {}
        self.overwrite_flags: list[bool] = []

    def list(self, prefix: str) -> list[str]:
        keys = super().list(prefix)
        me = threading.get_ident()
        if me not in self._first_listing:  # only each thread's first listing waits
            self._first_listing[me] = True
            self._gate.wait()
        return keys

    def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        self.overwrite_flags.append(overwrite)
        super().put_bytes(key, data, overwrite=overwrite)


def test_concurrent_archives_both_land_on_distinct_versions(tmp_path: Path) -> None:
    store = RacingStore(tmp_path, parties=2)
    keys: list[str] = []
    errors: list[BaseException] = []

    def run(tag: bytes) -> None:
        try:
            keys.append(archive_report(store, DAY, {"brief.html": tag, "model.json": tag + b"{}"}))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(t,)) for t in (b"one", b"two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors
    assert sorted(keys) == [
        "reports/date=2026-11-16/v1/brief.html",
        "reports/date=2026-11-16/v2/brief.html",
    ]
    assert {store.get_bytes(k) for k in keys} == {b"one", b"two"}
    for k in keys:  # each version holds one archiver's whole set, never a mix
        tag = store.get_bytes(k)
        assert store.get_bytes(k.replace("brief.html", "model.json")) == tag + b"{}"
    assert store.overwrite_flags
    assert not any(store.overwrite_flags)


def test_archive_gives_up_after_bounded_attempts(tmp_path: Path) -> None:
    class AlwaysTaken(LocalBackend):
        puts = 0

        def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
            self.puts += 1
            raise KeyExistsError(key)

    store = AlwaysTaken(tmp_path)
    with pytest.raises(KeyExistsError):
        archive_report(store, DAY, {"brief.html": b"x"})
    assert store.puts == 5
