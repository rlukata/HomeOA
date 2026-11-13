"""Board to-do items for failed and bounced deliveries (rendered by the Home to-do list).

An item stays until the board marks it handled (``notify:delivery_handled``), which sets
``Delivery.resolved_at``; a retried delivery that is sent leaves the failed list too.
"""

from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from .consent import ATTACHMENT_FAILURE
from .models import Delivery


def _handled(d) -> dict:
    return {
        "post_url": reverse("notify:delivery_handled", args=[d.pk]),
        "post_data": {},
        "action": _("Mark handled"),
    }


def _bounced(d) -> dict:
    unit = d.notification.unit
    if unit is not None:
        title = _("Email to unit %(unit)s bounced") % {"unit": unit.number}
        url = reverse("notify:unit_comms", args=[unit.number])
    else:
        title = _("Email to %(person)s bounced") % {"person": d.notification.recipient}
        url = reverse("registry:person", args=[d.notification.recipient_id])
    return {
        "id": f"notify-bounced-{d.pk}",
        "kind": "requests",
        "title": title,
        "sub": _("%(person)s · %(address)s · %(subject)s · %(error)s")
        % {
            "person": d.notification.recipient,
            "address": d.address,
            "subject": d.notification.subject,
            "error": d.error,
        },
        "url": url,
        "due": d.bounced_at.date() if d.bounced_at else None,
        **_handled(d),
    }


def todo_items(association) -> list[dict]:
    open_ = Delivery.all_associations.filter(
        association=association, resolved_at__isnull=True
    ).select_related("notification__recipient", "notification__unit")
    failed = open_.filter(status=Delivery.Status.FAILED).order_by("failed_at", "pk")
    bounced = open_.filter(status=Delivery.Status.BOUNCED).order_by("bounced_at", "pk")
    items = [
        {
            "id": f"notify-failed-{d.pk}",
            "kind": "ops",
            "title": (
                _("Email attachment missing: %(subject)s")
                if d.failure_kind == ATTACHMENT_FAILURE
                else _("Delivery failed: %(subject)s")
            )
            % {"subject": d.notification.subject},
            "sub": _("%(person)s · %(channel)s · %(error)s")
            % {"person": d.notification.recipient, "channel": d.channel, "error": d.error},
            "due": d.failed_at.date() if d.failed_at else None,
            **_handled(d),
        }
        for d in failed
    ]
    items.extend(_bounced(d) for d in bounced)
    items.extend(consent_items(association))
    return items


def consent_items(association) -> list[dict]:
    """E-notice consent ended by the system (two failed emails, or an imported flag
    that is not consent). Each stays until a newer record supersedes it (the owner consents
    again, or the board records a choice on the person page)."""
    from homeoa.registry.models import NoticePreference
    from homeoa.registry.services import current_notice_preference

    reasons = (NoticePreference.RevokeReason.FAILURES, NoticePreference.RevokeReason.IMPORT)
    revoked = (
        NoticePreference.all_associations.filter(association=association, revoke_reason__in=reasons)
        .select_related("person")
        .order_by("recorded_at", "pk")
    )
    failures, imported = [], []
    for pref in revoked:
        current = current_notice_preference(pref.person)
        if current is None or current.pk != pref.pk:
            continue
        (failures if pref.revoke_reason == reasons[0] else imported).append(pref)
    items = [
        {
            "id": f"notify-consent-revoked-{p.pk}",
            "kind": "requests",
            "title": _("Email consent ended for %(person)s: notices now go by mail")
            % {"person": p.person},
            "sub": _("Two emails in a row failed. Check the address with the owner."),
            "url": reverse("registry:person", args=[p.person_id]),
            "due": p.recorded_at.date(),
        }
        for p in failures
    ]
    if imported:
        items.append(
            {
                "id": "notify-consent-imported",
                "kind": "requests",
                "title": ngettext(
                    "%(n)s owner needs to consent again to get notices by email",
                    "%(n)s owners need to consent again to get notices by email",
                    len(imported),
                )
                % {"n": len(imported)},
                "sub": _(
                    "Imported e-delivery is not consent; they get mail until they agree: %(names)s"
                )
                % {"names": ", ".join(str(p.person) for p in imported)},
                "url": reverse("registry:person", args=[imported[0].person_id]),
                "due": imported[0].recorded_at.date(),
            }
        )
    return items
