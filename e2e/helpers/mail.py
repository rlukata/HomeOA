"""Mail capture. The server runs as a subprocess, so Django's locmem outbox is
not visible to tests. The E2E server uses Django's filebased EmailBackend writing to
$HOMEOA_E2E_MAIL_DIR (default .work/e2e-mail), and the suite reads those files.
"""

from __future__ import annotations

import email
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

# Magic link: /login/verify/<token>/.
LINK_RE = re.compile(r"https?://[^\s\"'<>]+/login/verify/[^\s\"'<>]+")


@dataclass
class Message:
    to: list[str]
    subject: str
    body: str
    attachments: tuple[tuple[str, str], ...] = ()  # (filename, content type)

    def first_link(self, pattern: re.Pattern[str] = LINK_RE) -> str:
        m = pattern.search(self.body)
        assert m, f"no link in message: {self.body[:500]}"
        return m.group(0)


class Mailbox:
    @staticmethod
    def from_env() -> Mailbox:
        return FileMailbox()

    def clear(self) -> None:
        raise NotImplementedError

    def messages(self) -> list[Message]:
        raise NotImplementedError

    def wait_for(self, to: str, timeout: float = 10) -> Message:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for m in reversed(self.messages()):
                if to.lower() in (a.lower() for a in m.to):
                    return m
            time.sleep(0.25)
        raise AssertionError(f"no mail to {to} within {timeout}s")


class FileMailbox(Mailbox):
    def __init__(self, path: str | None = None):
        self.dir = Path(
            path
            or os.environ.get(
                "HOMEOA_E2E_MAIL_DIR",
                str(Path(__file__).resolve().parents[2] / ".work" / "e2e-mail"),
            )
        )
        self.dir.mkdir(parents=True, exist_ok=True)

    def clear(self) -> None:
        for f in self.dir.glob("*.log"):
            f.unlink()

    def messages(self) -> list[Message]:
        out = []
        for f in sorted(self.dir.glob("*.log")):
            for raw in f.read_text().split("-" * 79):
                if not raw.strip():
                    continue
                msg = email.message_from_string(raw.strip())
                body = "\n".join(
                    (p.get_payload(decode=True) or b"").decode(p.get_content_charset() or "utf-8")
                    for p in msg.walk()
                    if not p.is_multipart() and not p.get_filename()
                )
                files = tuple(
                    (p.get_filename(), p.get_content_type()) for p in msg.walk() if p.get_filename()
                )
                out.append(
                    Message(
                        to=[a.strip() for a in (msg["To"] or "").split(",")],
                        subject=msg["Subject"] or "",
                        body=str(body),
                        attachments=files,
                    )
                )
        return out
