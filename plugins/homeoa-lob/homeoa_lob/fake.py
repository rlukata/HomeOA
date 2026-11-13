"""In-repo fake of the Lob letters API. No network, no keys, nothing is mailed."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets


class FakeLob:
    """Records letters in memory (class-wide) and answers like ``POST /v1/letters``."""

    letters: dict[str, dict] = {}

    def create_letter(self, *, to: dict, file: bytes, extra_service: str = "", description=""):
        if not file:
            raise ValueError("file is required")
        letter_id = "ltr_" + secrets.token_hex(10)
        data = {
            "id": letter_id,
            "description": description,
            "to": to,
            "extra_service": extra_service or None,
            "tracking_number": (
                "9407" + "".join(str(secrets.randbelow(10)) for _ in range(18))
                if extra_service == "certified"
                else None
            ),
            "expected_delivery_date": (dt.date.today() + dt.timedelta(days=5)).isoformat(),
            "sha256": hashlib.sha256(file).hexdigest(),
        }
        self.letters[letter_id] = data
        return data

    @classmethod
    def reset(cls) -> None:
        cls.letters.clear()


def event(letter_id: str, event_type: str, event_id: str = "") -> bytes:
    """A Lob-shaped webhook body for ``letter_id``."""
    return json.dumps(
        {
            "id": event_id or "evt_" + secrets.token_hex(10),
            "event_type": {"id": event_type, "object": "event"},
            "body": {"id": letter_id, "object": "letter"},
            "object": "event",
        }
    ).encode()


def sign(body: bytes, timestamp: str, secret: str) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
