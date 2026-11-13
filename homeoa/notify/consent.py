"""Automatic revocation of e-notice consent after failed email (RCW 64.34.396).

Two consecutive failed email deliveries to a person (bounced, or refused for the
recipient by the SMTP server) since their current consent end that consent: a new postal
record is appended with reason "failures" (audited by the registry service), notices fall
back to mail, and the board gets a to-do. Transport failures (auth error, outage,
timeout) and missing attachments are skipped: they neither count nor break a run of
bounces. The count is derived from the deliveries themselves, so a successful send in
between resets it without any counter to keep in sync.
"""

from __future__ import annotations

import logging

from .models import Delivery

logger = logging.getLogger(__name__)

FAILURE_LIMIT = 2
# A delivery failed because a file to attach was missing: not a channel failure.
ATTACHMENT_FAILURE = "attachment"
# The send failed before the server accepted it (auth, outage, timeout): not the address.
TRANSPORT_FAILURE = "transport"
# The SMTP server refused the recipient address: counts like a bounce.
RECIPIENT_FAILURE = "recipient"
_FAILED = (Delivery.Status.FAILED, Delivery.Status.BOUNCED)
_OUTCOMES = (*_FAILED, Delivery.Status.SENT, Delivery.Status.DELIVERED)


def _counts(d: Delivery) -> bool:
    """A real recipient failure: a bounce, or a failed send refused for the recipient."""
    return d.status == Delivery.Status.BOUNCED or (
        d.status == Delivery.Status.FAILED and d.failure_kind == RECIPIENT_FAILURE
    )


def _skipped(d: Delivery) -> bool:
    return d.status == Delivery.Status.FAILED and not _counts(d)


def _outcome_time(d: Delivery):
    return d.bounced_at or d.failed_at or d.delivered_at or d.sent_at or d.created_at


def consecutive_failures(person, since) -> int:
    """Failed email deliveries to ``person`` after ``since``, counted back from the newest
    outcome until the first success."""
    deliveries = list(
        Delivery.all_associations.filter(
            notification__recipient=person,
            channel="email",
            status__in=_OUTCOMES,
            created_at__gte=since,
        )
    )
    deliveries.sort(key=lambda d: (_outcome_time(d), d.pk), reverse=True)
    count = 0
    for d in deliveries:
        if _skipped(d):
            continue
        if not _counts(d):
            break
        count += 1
    return count


def after_email_outcome(delivery: Delivery):
    """Call after an email delivery failed or bounced. Revokes consent at the limit and
    returns the revoking record, else None."""
    from homeoa.registry.models import NoticePreference
    from homeoa.registry.services import current_notice_preference, revoke_consent

    if delivery.channel != "email" or not _counts(delivery):
        return None
    person = delivery.notification.recipient
    pref = current_notice_preference(person)
    if pref is None or not pref.e_delivery_consent:
        return None
    if consecutive_failures(person, pref.recorded_at) < FAILURE_LIMIT:
        return None
    logger.info("revoking e-notice consent for person %s after failed emails", person.pk)
    return revoke_consent(
        person,
        NoticePreference.RevokeReason.FAILURES,
        note=f"{FAILURE_LIMIT} consecutive failed emails",
    )
