"""Approval emails (FIN-12).

* ``ap.bill_awaiting_approval``: to each user who may approve, when a bill enters their
  queue (it is submitted and needs approval).
* ``ap.bill_decided``: to the person who entered the bill when it is fully approved or
  rejected.

Both are sent with ``notify_after_commit``: they run only after the bill's
transaction commits, and a failing send is logged and audited, never raised, so it can
never roll back or block the bill or ledger change. Users are mapped to their registry
``Person`` (one is created for a login with none, as ``notify.board_recipients`` does);
that lookup also runs after commit, so no write happens inside the money transaction.
One message per recipient per submission (the period is the accrual entry id).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext_lazy

from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

AWAITING_TYPE = "ap.bill_awaiting_approval"
DECIDED_TYPE = "ap.bill_decided"

logger = logging.getLogger(__name__)


def register_notification_types() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=AWAITING_TYPE,
            label=gettext_lazy("Bill waiting for your approval"),
            default_channels=(ch.EMAIL, ch.INAPP),
            # Internal workflow mail to the approver's own login, not an association notice.
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with b=bill_label %}"
                "Bill {{ b }} needs your approval{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with b=bill_label t=total s=submitter %}"
                "{{ s }} submitted bill {{ b }} for {{ t }}. It is waiting for your approval."
                "{% endblocktranslate %}\n\n{{ url }}"
            ),
        )
    )
    ntypes.register(
        ntypes.NotificationType(
            key=DECIDED_TYPE,
            label=gettext_lazy("Your bill was approved or rejected"),
            default_channels=(ch.EMAIL, ch.INAPP),
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% if approved %}{% blocktranslate with b=bill_label %}"
                "Bill {{ b }} was approved{% endblocktranslate %}{% else %}"
                "{% blocktranslate with b=bill_label %}Bill {{ b }} was rejected"
                "{% endblocktranslate %}{% endif %}"
            ),
            body_template=(
                "{% load i18n %}{% if approved %}"
                "{% blocktranslate with b=bill_label t=total %}Bill {{ b }} for {{ t }} is "
                "approved and can be paid.{% endblocktranslate %}{% else %}"
                "{% blocktranslate with b=bill_label d=decider %}{{ d }} rejected bill {{ b }}"
                " and returned it to draft.{% endblocktranslate %}\n"
                "{% blocktranslate with r=reason %}Reason: {{ r }}{% endblocktranslate %}"
                "{% endif %}\n\n{{ url }}"
            ),
        )
    )


def person_for(user, association):
    from homeoa.registry.models import Person

    person = (
        Person.all_associations.filter(association=association, user=user).order_by("pk").first()
    )
    if person is None:
        person = Person.all_associations.create(
            association=association,
            user=user,
            full_name=user.full_name or user.email,
            email=user.email,
        )
    return person


def _context(bill) -> dict:
    url = getattr(settings, "HOMEOA_BASE_URL", "") + reverse("ap:detail", args=[bill.pk])
    return {
        "bill_label": str(bill),
        "total": f"${bill.total:,.2f}",
        "url": url,
        "association_name": bill.association.name,
    }


def _name(user) -> str:
    return (getattr(user, "full_name", "") or getattr(user, "email", "")) if user else ""


def _after_commit(type_key, users, context, *, bill, period) -> None:
    """Resolve recipients and hand off to ``notify_after_commit`` once the bill commits.

    Never raises into the caller: a failure is logged and audited.
    """
    association = bill.association
    user_ids = [u.pk for u in users]

    def _run() -> None:
        try:
            from django.contrib.auth import get_user_model

            from homeoa.notify.services import notify_after_commit

            people = [
                person_for(u, association)
                for u in get_user_model().objects.filter(pk__in=user_ids).order_by("pk")
            ]
            notify_after_commit(
                type_key, people, context, obj=bill, period=period, association=association
            )
        except Exception as exc:
            logger.exception("Approval notification %s failed", type_key)
            try:
                from homeoa import audit

                audit.record(
                    "notify.send_failed",
                    bill,
                    association=association,
                    message=f"{type_key}: {exc}"[:500],
                )
            except Exception:
                logger.exception("Could not audit the failed notification %s", type_key)

    transaction.on_commit(_run)


def notify_approvers(bill, users, submitter=None) -> None:
    """Queue the awaiting-approval message to ``users`` after commit."""
    users = [u for u in users if u is not None]
    if not users or not bill.entry_id:
        return
    _after_commit(
        AWAITING_TYPE,
        users,
        {**_context(bill), "submitter": _name(submitter or bill.created_by)},
        bill=bill,
        period=f"entry:{bill.entry_id}",
    )


def notify_submitter(bill, *, approved: bool, entry_id, decider=None, reason="") -> None:
    """Queue the decision message to the bill's submitter after commit."""
    submitter = bill.created_by
    if submitter is None or not submitter.is_active:
        return
    if decider is not None and decider.pk == submitter.pk:
        return
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_any

    # Only a submitter who still has bills access today (a current role granting it).
    if not has_any(
        submitter, (perms.FINANCE_MANAGE, perms.BILLS_APPROVE), association=bill.association
    ):
        return
    _after_commit(
        DECIDED_TYPE,
        [submitter],
        {**_context(bill), "approved": approved, "decider": _name(decider), "reason": reason},
        bill=bill,
        period=f"entry:{entry_id}:{'approved' if approved else 'rejected'}",
    )
