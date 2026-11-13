"""Expiry and auto-renew alerts for vendor certificates, licenses and contracts (VND-02/03).

The daily job ``vendors.expiry_alerts`` calls ``run(association, as_of)``, which is
idempotent: it raises at most one ``VendorAlert`` per document and per contract term.

* Certificate or license: raised ``HOMEOA_VENDOR_DOC_WARNING_DAYS`` (default 30) days before
  its expiry; resolved when a later one of the same kind is added, or dismissed.
* Auto-renewing contract: raised on its notice date (term end minus notice-to-cancel days),
  or ``HOMEOA_VENDOR_CONTRACT_LEAD_DAYS`` (default 0) earlier; open until marked decided or
  the term ends.

"Send request" emails the vendor's primary contact. Vendor contacts are not registry
people, so the message is rendered from the registered notification type
``vendors.document_request`` and handed straight to the ``EmailChannel`` provider; each
send is kept as a ``DocumentRequest`` row and audited.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit
from homeoa.notify import types as ntypes

from .models import Contract, DocumentRequest, Vendor, VendorAlert, VendorDocument

logger = logging.getLogger(__name__)

REQUEST_TYPE = "vendors.document_request"


def doc_warning_days() -> int:
    return int(getattr(settings, "HOMEOA_VENDOR_DOC_WARNING_DAYS", 30))


def contract_lead_days() -> int:
    return int(getattr(settings, "HOMEOA_VENDOR_CONTRACT_LEAD_DAYS", 0))


def document_alert_on(expiry: dt.date) -> dt.date:
    """The day a certificate or license starts warning (the to-do job and the list)."""
    return expiry - dt.timedelta(days=doc_warning_days())


def contract_alert_on(contract: Contract) -> dt.date:
    """The day a contract's renewal decision starts warning."""
    return contract.notice_date - dt.timedelta(days=contract_lead_days())


def document_state(expiry: dt.date, as_of: dt.date) -> str:
    """Badge kind for a certificate: ``danger`` expired, ``warn`` in the window, else ``ok``."""
    if expiry < as_of:
        return "danger"
    return "warn" if as_of >= document_alert_on(expiry) else "ok"


def contract_state(contract: Contract, as_of: dt.date) -> str:
    """Badge kind for a contract term: ``danger`` ended, ``warn`` in the notice window."""
    if contract.end_date < as_of:
        return "danger"
    return "warn" if as_of >= contract_alert_on(contract) else "ok"


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=REQUEST_TYPE,
            label=gettext_lazy("Request for a new vendor certificate"),
            default_channels=("email",),
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with a=association_name k=kind_label %}"
                "{{ a }}: please send an updated {{ k }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with n=contact_name %}Hello {{ n }},"
                "{% endblocktranslate %}\n\n"
                "{% blocktranslate with k=kind_label v=vendor_name d=expiry a=association_name %}"
                "The {{ k }} we have on file for {{ v }} expires on {{ d }}. Please send "
                "{{ a }} an updated copy.{% endblocktranslate %}\n\n"
                "{% translate 'Thank you.' %}\n{{ association_name }}"
            ),
        )
    )


def current_documents(association):
    """The latest certificate of each kind per active vendor."""
    seen, out = set(), []
    docs = VendorDocument.all_associations.filter(
        association=association, vendor__status=Vendor.Status.ACTIVE
    ).order_by("vendor_id", "kind", "-expiry_date", "-pk")
    for doc in docs.select_related("vendor"):
        if (doc.vendor_id, doc.kind) not in seen:
            seen.add((doc.vendor_id, doc.kind))
            out.append(doc)
    return out


def _raise(association, kind, key, as_of, due, **target) -> VendorAlert | None:
    alert, created = VendorAlert.all_associations.get_or_create(
        association=association,
        key=key,
        defaults={"kind": kind, "due": due, "raised_on": as_of, **target},
    )
    return alert if created else None


@transaction.atomic
def run(association, as_of: dt.date) -> list[VendorAlert]:
    """Raise due alerts and close stale ones. Returns the alerts raised now."""
    raised = []
    current = current_documents(association)
    for doc in current:
        if as_of >= document_alert_on(doc.expiry_date):
            alert = _raise(
                association,
                VendorAlert.Kind.DOCUMENT,
                f"doc-{doc.pk}",
                as_of,
                doc.expiry_date,
                document=doc,
            )
            if alert:
                raised.append(alert)
    VendorAlert.all_associations.filter(
        association=association, kind=VendorAlert.Kind.DOCUMENT, resolved_on__isnull=True
    ).exclude(document__in=current).update(resolved_on=as_of)

    contracts = Contract.all_associations.filter(
        association=association,
        auto_renew=True,
        end_date__gte=as_of,
        vendor__status=Vendor.Status.ACTIVE,
    )
    for contract in contracts:
        if as_of >= contract_alert_on(contract):
            alert = _raise(
                association,
                VendorAlert.Kind.RENEWAL,
                f"contract-{contract.pk}-{contract.end_date.isoformat()}",
                as_of,
                contract.notice_date,
                contract=contract,
            )
            if alert:
                raised.append(alert)
    VendorAlert.all_associations.filter(
        association=association,
        kind=VendorAlert.Kind.RENEWAL,
        resolved_on__isnull=True,
        contract__end_date__lt=as_of,
    ).update(resolved_on=as_of)
    return raised


def resolve(alert: VendorAlert, as_of: dt.date, user=None) -> VendorAlert:
    if alert.resolved_on is None:
        alert.resolved_on = as_of
        alert.save(update_fields=["resolved_on"])
        audit.record("vendors.alert_resolved", alert, actor=user)
    return alert


def expired_vendor_ids(association, as_of: dt.date) -> set[int]:
    """Vendors whose current certificate or license has expired (red badge on the list)."""
    return {d.vendor_id for d in current_documents(association) if d.expiry_date < as_of}


def todo_items(association) -> list[dict]:
    alerts = VendorAlert.all_associations.filter(
        association=association, resolved_on__isnull=True
    ).select_related("document__vendor", "contract__vendor")
    items = []
    for alert in alerts:
        if alert.kind == VendorAlert.Kind.DOCUMENT and alert.document:
            doc = alert.document
            title = (
                _("Request new insurance certificate: %(v)s")
                if doc.kind == VendorDocument.Kind.COI
                else _("Request renewed license: %(v)s")
            ) % {"v": doc.vendor.name}
            item = {
                "id": f"vendor-alert-{alert.pk}",
                "kind": "deadline",
                "title": title,
                "sub": _("Expires %(d)s") % {"d": doc.expiry_date.isoformat()},
                "due": alert.due,
                "url": reverse("vendors:detail", args=[doc.vendor.slug]),
                "action": _("Open"),
            }
            last = doc.requests.filter(sent=True).order_by("-sent_at").first()
            if last is not None:
                item["sub"] += " · " + _("requested %(d)s") % {"d": last.sent_at.date().isoformat()}
            elif doc.vendor.primary_contact and doc.vendor.primary_contact.email:
                item |= {
                    "action": _("Send request"),
                    "post_url": reverse("vendors:document_request", args=[doc.pk]),
                    "post_data": {},
                }
            items.append(item)
        elif alert.kind == VendorAlert.Kind.RENEWAL and alert.contract:
            c = alert.contract
            items.append(
                {
                    "id": f"vendor-alert-{alert.pk}",
                    "kind": "deadline",
                    "title": _("Decide on renewal: %(t)s") % {"t": c.title},
                    "sub": _("%(v)s renews automatically on %(end)s; cancel by %(d)s")
                    % {
                        "v": c.vendor.name,
                        "end": c.end_date.isoformat(),
                        "d": c.notice_date.isoformat(),
                    },
                    "due": alert.due,
                    "url": reverse("vendors:detail", args=[c.vendor.slug]),
                    "action": _("Mark decided"),
                    "post_url": reverse("vendors:alert_resolve", args=[alert.pk]),
                    "post_data": {},
                }
            )
    return items


def send_document_request(document: VendorDocument, user=None) -> DocumentRequest:
    """Email the vendor's primary contact for a new certificate and record it."""
    from homeoa import providers
    from homeoa.notify.services import render
    from homeoa.providers.email import EmailRequest

    vendor = document.vendor
    contact = vendor.primary_contact
    if contact is None or not contact.email:
        raise ValueError(_("This vendor has no contact with an email address."))
    association = document.association
    msg = render(
        ntypes.get(REQUEST_TYPE),
        {
            "association_name": association.name,
            "contact_name": contact.name,
            "vendor_name": vendor.name,
            "kind_label": str(document.get_kind_display()).lower(),
            "expiry": document.expiry_date.isoformat(),
        },
    )
    req = DocumentRequest.all_associations.create(
        association=association,
        document=document,
        contact=contact,
        email=contact.email,
        subject=msg.subject[:300],
        body=msg.body,
        requested_by=user if getattr(user, "pk", None) else None,
    )
    try:
        result = providers.get_provider("EmailChannel", association).send(
            EmailRequest(to=(contact.email,), subject=msg.subject, text=msg.body + "\n")
        )
        req.sent, req.message_id, req.error = result.sent, result.message_id, result.error
    except Exception as exc:  # a provider error is recorded, not raised
        logger.warning("vendor document request %s failed: %s", req.pk, exc)
        req.sent, req.error = False, str(exc)
    req.save(update_fields=["sent", "message_id", "error"])
    audit.record("vendors.document_requested", document, actor=user)
    return req
