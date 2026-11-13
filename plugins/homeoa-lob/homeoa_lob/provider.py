"""``LobPostal``: the ``PostalProvider`` this plugin registers (fake client only)."""

from __future__ import annotations

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.providers.postal import LetterRequest, LetterResult, LetterStatus, PostalProvider

from .fake import FakeLob


class LobPostal(PostalProvider):
    prints_locally = False
    name = gettext_lazy("Lob (test fake)")

    def __init__(self, installation=None, client=None):
        self.installation = installation
        # Only the fake exists; a live client needs sandbox keys from the user first.
        self.client = client or FakeLob()

    def send_letter(self, letter: LetterRequest) -> LetterResult:
        to = letter.to
        try:
            data = self.client.create_letter(
                to={
                    "name": to.name,
                    "address_line1": to.line1,
                    "address_line2": to.line2,
                    "address_city": to.city,
                    "address_state": to.region,
                    "address_zip": to.postal_code,
                    "address_country": to.country,
                },
                file=letter.pdf,
                extra_service="certified" if letter.certified else "",
                description=letter.description,
            )
        except Exception as exc:
            return LetterResult(status=LetterStatus.FAILED, message=str(exc)[:500])
        return LetterResult(
            status=LetterStatus.SUBMITTED,
            reference=data["id"],
            tracking=data.get("tracking_number") or "",
            message=_("Sent to Lob."),
        )
