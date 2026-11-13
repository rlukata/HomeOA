"""Home to-dos: an envelope was completed or declined and the board has not reviewed it."""

from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms

from . import services
from .models import Envelope, SignatureEvent


def todo_items(association) -> list[dict]:
    items = []
    for env in services.needs_review(association):
        if env.status == Envelope.Status.DECLINED:
            ev = (
                env.events.filter(kind=SignatureEvent.Kind.DECLINED).select_related("signer").last()
            )
            who = ev.signer.name if ev and ev.signer else ""
            reason = (ev.data.get("reason") if ev else "") or ""
            title = _("Signature declined: %(title)s") % {"title": env.title}
            sub = _("Declined by %(name)s.") % {"name": who} if who else _("A signer declined.")
            if reason:
                sub = f"{sub} {reason}"
        else:
            title = _("Signed: %(title)s") % {"title": env.title}
            sub = _("Every signer has signed. The signed PDF with its certificate is ready.")
        items.append(
            {
                "id": f"esign-review-{env.pk}",
                "kind": "gov",
                "title": title,
                "sub": sub,
                "url": reverse("esign:detail", args=[env.pk]),
                "action": _("Mark reviewed"),
                "post_url": reverse("esign:reviewed", args=[env.pk]),
                "perm": perms.DOCUMENTS_MANAGE,
            }
        )
    return items
