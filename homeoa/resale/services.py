"""Resale certificates (RES-02).

A request comes from the selling owner in the portal or is logged by the board for an
agent or escrow. Opening it fixes the certificate form in force (rule pack
``resale.certificate_form``), the due date (``resale.certificate_days``) and drafts every
answer HomeOA can pre-fill (:mod:`.prefill`). The board edits the answers, then issues:
the PDF is stored and emailed to the requester and the seller, and the fee (never above
``resale.fee_cap``) posts as a charge on the seller's account. Nothing is sent before the
board issues it.

A statement of unpaid assessments (``kind="statement"``, WA: RCW 64.34.364(15)) follows the
same steps with the ``unpaid_statement`` form, the deadline ``statements.unpaid_assessments_days``
and no fee cap ("reasonable charges").
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit, rules
from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

from . import catalog, prefill
from .models import ResaleRequest

REQUESTED_TYPE = "resale.requested"
CERTIFICATE_TYPE = "resale.certificate"


class ResaleError(ValidationError):
    pass


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=REQUESTED_TYPE,
            label=gettext_lazy("Resale certificate requested"),
            default_channels=(ch.EMAIL, ch.INAPP),
            audience=ntypes.AUDIENCE_BOARD,
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with u=unit d=document %}"
                "{{ d }} requested for unit {{ u }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with n=name d=due doc=document %}"
                "{{ n }} asked for a document: {{ doc }}. It is due {{ d }}.{% endblocktranslate %}"
                "\n\n{{ url }}"
            ),
        )
    )
    ntypes.register(
        ntypes.NotificationType(
            key=CERTIFICATE_TYPE,
            label=gettext_lazy("Resale certificate"),
            default_channels=(ch.EMAIL,),
            # Requested by the seller or their agent; not an association notice.
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with u=unit a=association_name d=document %}"
                "{{ d }}: unit {{ u }}, {{ a }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with u=unit d=document %}"
                "Attached: {{ d }} for unit {{ u }}.{% endblocktranslate %}"
            ),
        )
    )


STATEMENT = ResaleRequest.Kind.STATEMENT
CERTIFICATE = ResaleRequest.Kind.CERTIFICATE
DAYS_KEY = {CERTIFICATE: "resale.certificate_days", STATEMENT: "statements.unpaid_assessments_days"}


def form_for(association, on: dt.date, kind=CERTIFICATE):
    if kind == STATEMENT:
        return catalog.UNPAID_STATEMENT
    form_id = rules.value(association, "resale.certificate_form", on)
    return catalog.FORMS.get(form_id) if form_id else None


def fee_cap(association, on: dt.date, kind=CERTIFICATE) -> Decimal | None:
    """The rule pack's cap for a certificate; a statement's fee is only "reasonable"."""
    return None if kind == STATEMENT else rules.value(association, "resale.fee_cap", on)


@transaction.atomic
def open_request(
    association,
    *,
    unit,
    on: dt.date,
    send_to: str,
    seller=None,
    requester_name: str = "",
    note="",
    kind=CERTIFICATE,
) -> ResaleRequest:
    form = form_for(association, on, kind)
    days = rules.value(association, DAYS_KEY[kind], on)
    return ResaleRequest.all_associations.create(
        association=association,
        kind=kind,
        unit=unit,
        seller=seller,
        requester_name=requester_name.strip(),
        send_to=send_to.strip(),
        received_on=on,
        due_on=on + dt.timedelta(days=int(days)) if days is not None else None,
        form_id=form.id if form else "",
        answers=prefill.draft(form, unit, on) if form else {},
        note=note.strip(),
    )


def submit(
    association, *, unit, seller, send_to: str, on: dt.date, note="", kind=CERTIFICATE
) -> ResaleRequest:
    """The owner asks in the portal; the board is told."""
    from homeoa.notify.services import board_recipients, notify_after_commit

    req = open_request(
        association, unit=unit, on=on, send_to=send_to, seller=seller, note=note, kind=kind
    )
    notify_after_commit(
        REQUESTED_TYPE,
        board_recipients(association),
        {
            "document": req.get_kind_display(),
            "unit": unit.number,
            "name": seller.full_name,
            "due": req.due_on.isoformat() if req.due_on else "",
            "url": getattr(settings, "HOMEOA_BASE_URL", "")
            + reverse("resale:detail", args=[req.pk]),
            "association_name": association.name,
        },
        obj=req,
        association=association,
    )
    return req


def items(req: ResaleRequest) -> list[dict]:
    form = catalog.FORMS.get(req.form_id)
    if form is None:
        return []
    rows = []
    for i in form.items:
        answer = req.answers.get(i.key, "")
        note = prefill.off_note(i.source) if i.source and i.source not in prefill.SOURCES else ""
        rows.append(
            {
                "key": i.key,
                "label": i.label,
                "prefilled": bool(i.source),
                "answer": answer,
                # Still the "not tracked in HomeOA" note: the board has to answer it.
                "needs_answer": bool(note) and answer == note,
            }
        )
    return rows


def save_answers(req: ResaleRequest, answers: dict[str, str], *, user=None) -> ResaleRequest:
    if req.status != ResaleRequest.Status.OPEN:
        raise ResaleError(_("This request is no longer open."))
    keys = {i["key"] for i in items(req)}
    req.answers = {k: (answers.get(k) or "").strip() for k in keys}
    req.save(update_fields=["answers"])
    return req


def _pdf(req: ResaleRequest, on: dt.date, signer: str = "") -> bytes:
    from homeoa.letters.letterhead import letterhead
    from homeoa.letters.pdf import build_pdf

    form = catalog.get(req.form_id)
    rows = [[_("Item"), _("Answer")]]
    for i in items(req):
        rows.append([f"({i['key']}) {i['label']}", i["answer"]])
    intro = _(
        "%(document)s for %(unit)s, prepared by the association on %(date)s from its books "
        "and records at the request of %(who)s."
    ) % {
        "document": req.get_kind_display(),
        "unit": req.unit,
        "date": on.isoformat(),
        "who": req.seller.full_name if req.seller else req.requester_name,
    }
    cite = form.citation or rules.resolve(req.association, DAYS_KEY[req.kind], on).citation
    if cite:
        intro += " " + _("Furnished under %(cite)s.") % {"cite": cite}
    if signer:
        intro += "\n\n" + _("Signed for the association by %(name)s.") % {"name": signer}
    return build_pdf(
        letterhead=letterhead(req.association),
        address_lines=[
            req.requester_name or (req.seller.full_name if req.seller else ""),
            req.send_to,
        ],
        date_text=f"{on:%B} {on.day}, {on.year}",
        subject=f"{req.get_kind_display()}: {req.unit}",
        body=intro,
        tables=[{"rows": rows}],
    )


@transaction.atomic
def issue(req: ResaleRequest, *, fee: Decimal, user, on: dt.date) -> ResaleRequest:
    """Store and send the certificate; post the fee to the seller's account."""
    from homeoa.billing.services.adjustments import manual_charge
    from homeoa.notify.services import notify_after_commit
    from homeoa.providers import get_provider
    from homeoa.registry.services import find_or_create_person, ownership_period_on

    req = ResaleRequest.all_associations.select_for_update().get(pk=req.pk)
    if req.status != ResaleRequest.Status.OPEN:
        raise ResaleError(_("This request is no longer open."))
    if req.form_id not in catalog.FORMS:
        raise ResaleError(_("There is no certificate form for this date. Fill it by hand."))
    missing = [i["key"] for i in items(req) if not i["answer"]]
    if missing:
        raise ResaleError(
            _("Answer every item before issuing (write None if nothing applies): %(keys)s.")
            % {"keys": ", ".join(missing)}
        )
    untracked = [i["key"] for i in items(req) if i["needs_answer"]]
    if untracked:
        raise ResaleError(
            _("Answer the items HomeOA does not track before issuing: %(keys)s.")
            % {"keys": ", ".join(untracked)}
        )
    cap = fee_cap(req.association, on, req.kind)
    if fee < 0 or (cap is not None and fee > cap):
        raise ResaleError(_("The fee can be at most %(cap)s.") % {"cap": cap})
    association = req.association
    stored = get_provider("FileStorage", association).put(
        association.pk,
        f"{filename(req)}-{on.isoformat()}.pdf",
        _pdf(req, on, _signer(user)),
        content_type="application/pdf",
        folder="resale",
    )
    req.pdf_key = stored.key
    req.fee = fee
    if fee > 0:
        req.charge = manual_charge(
            req.unit,
            fee,
            _("%(document)s fee") % {"document": req.get_kind_display()},
            req.get_kind_display(),
            date=on,
            ownership_period=ownership_period_on(req.unit, on),
            posted_by=user,
        )
    req.status = ResaleRequest.Status.ISSUED
    req.issued_on = on
    req.issued_by = user
    req.save()
    recipients = [
        find_or_create_person(association, req.requester_name or req.send_to, req.send_to)
    ]
    if req.seller and req.seller.email and req.seller.pk != recipients[0].pk:
        recipients.append(req.seller)
    notify_after_commit(
        CERTIFICATE_TYPE,
        recipients,
        {
            "unit": req.unit.number,
            "association_name": association.name,
            "document": req.get_kind_display(),
        },
        on,
        obj=req,
        association=association,
        force_channels=(ch.EMAIL,),
        attachments=[
            {
                "name": f"{filename(req)}.pdf",
                "content_type": "application/pdf",
                "file_key": stored.key,
            }
        ],
    )
    audit.record(
        "resale.issued", req, actor=user, changes={"fee": str(fee), "send_to": req.send_to}
    )
    return req


def filename(req: ResaleRequest) -> str:
    base = "unpaid-assessments" if req.is_statement else "resale-certificate"
    return f"{base}-{req.unit.number}"


def _signer(user) -> str:
    """The officer or agent signing: the issuing board member's name (statute: "signed by
    an officer or authorized agent of the association")."""
    if user is None or not getattr(user, "pk", None):
        return ""
    return user.full_name or user.email


@transaction.atomic
def cancel(req: ResaleRequest, *, user, reason: str) -> ResaleRequest:
    if req.status != ResaleRequest.Status.OPEN:
        raise ResaleError(_("This request is no longer open."))
    req.status = ResaleRequest.Status.CANCELLED
    req.note = (reason or "").strip()[:500]
    req.save(update_fields=["status", "note"])
    return req


def pdf_bytes(req: ResaleRequest) -> bytes:
    from homeoa.providers import get_provider

    return get_provider("FileStorage", req.association).get(req.pdf_key)


def todo_items(association) -> list[dict]:
    from homeoa.access import permissions as perms

    out = []
    for req in ResaleRequest.all_associations.filter(
        association=association, status=ResaleRequest.Status.OPEN
    ).select_related("unit"):
        out.append(
            {
                "id": f"resale-{req.pk}",
                "kind": "deadline",
                "title": _("%(document)s for %(unit)s")
                % {"document": req.get_kind_display(), "unit": req.unit},
                "sub": _("Due %(date)s") % {"date": req.due_on.isoformat()} if req.due_on else "",
                "due": req.due_on,
                "url": reverse("resale:detail", args=[req.pk]),
                "action": _("Open"),
                "perm": perms.UNITS_MANAGE,
            }
        )
    return out
