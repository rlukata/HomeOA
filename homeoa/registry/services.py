"""Registry service API: the only supported way to change ownership and consent.

Lookups (all take an optional ``on`` date, default today in the association's time zone):

* ``ownership_period_on(unit, on)``: the :class:`OwnershipPeriod` covering ``on`` or None.
* ``current_owners(unit, on)``: owners (Person list, primary first) on that date.
* ``units_owned_by(person, on)``: units the person owns on that date.
* ``current_occupants(unit, on, kind=None)``: occupancy rows covering that date.
* ``current_notice_preference(person)``: newest :class:`NoticePreference` or None.
* ``interest_total(association)`` / ``interest_total_ok(association)``.
* ``vote_weight(unit)``: per the association's ``settings["vote_weighting"]``.

Writes: ``transfer_ownership`` (checked by hooks added with
``register_transfer_validator``), ``set_initial_ownership``, ``add_occupancy``,
``record_notice_preference``. Each runs in a transaction and is audited (models are
registered with :mod:`homeoa.audit`); transfers publish ``ownership.transferred``.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import audit, events
from homeoa.core.context import association_today

from .events import NoticePreferenceChanged, OwnershipTransferred
from .models import NoticePreference, Occupancy, OwnershipPeriod, OwnershipShare, Person, Unit

HUNDRED = Decimal("100")

VOTE_PER_UNIT = "per_unit"
VOTE_BY_INTEREST = "allocated_interest"
VOTE_CUSTOM = "custom"
VOTE_WEIGHTINGS = (VOTE_PER_UNIT, VOTE_BY_INTEREST, VOTE_CUSTOM)


# Kept as a short alias: registry views call ``services.today_for``.
today_for = association_today


def _on(unit_or_person, on):
    return on or association_today(unit_or_person.association)


# ---------------------------------------------------------------- lookups


def ownership_period_on(unit: Unit, on: datetime.date | None = None) -> OwnershipPeriod | None:
    on = _on(unit, on)
    return (
        OwnershipPeriod.all_associations.filter(unit=unit, start__lte=on)
        .exclude(end__lt=on)
        .order_by("-start")
        .first()
    )


def current_owners(unit: Unit, on: datetime.date | None = None) -> list[Person]:
    period = ownership_period_on(unit, on)
    if period is None:
        return []
    return [s.person for s in period.shares.select_related("person")]


def units_owned_by(person: Person, on: datetime.date | None = None) -> list[Unit]:
    on = _on(person, on)
    shares = (
        OwnershipShare.all_associations.filter(person=person, period__start__lte=on)
        .exclude(period__end__lt=on)
        .select_related("period__unit")
    )
    return sorted({s.period.unit for s in shares}, key=lambda u: u.sort_key)


def current_occupants(
    unit: Unit, on: datetime.date | None = None, kind: str | None = None
) -> list[Occupancy]:
    on = _on(unit, on)
    qs = (
        Occupancy.all_associations.filter(unit=unit, start__lte=on)
        .exclude(end__lt=on)
        .select_related("person")
    )
    if kind:
        qs = qs.filter(kind=kind)
    return list(qs)


def current_notice_preference(person: Person) -> NoticePreference | None:
    return (
        NoticePreference.all_associations.filter(person=person)
        .order_by("-recorded_at", "-pk")
        .first()
    )


def interest_total(association) -> Decimal:
    total = Unit.all_associations.filter(association=association).aggregate(
        t=Sum("allocated_interest")
    )["t"]
    return total if total is not None else Decimal("0")


def interest_total_ok(association) -> bool:
    return interest_total(association) == HUNDRED


def vote_weighting(association) -> str:
    value = (association.settings or {}).get("vote_weighting", VOTE_PER_UNIT)
    return value if value in VOTE_WEIGHTINGS else VOTE_PER_UNIT


def vote_weight(unit: Unit) -> Decimal:
    mode = vote_weighting(unit.association)
    if mode == VOTE_BY_INTEREST:
        return unit.allocated_interest
    if mode == VOTE_CUSTOM:
        return unit.vote_weight
    return Decimal("1")


# ---------------------------------------------------------------- writes

Owners = Sequence[Person] | Sequence[tuple[Person, Decimal]]

# Callables (unit, owners, effective_date) that raise ValidationError to block a transfer.
_transfer_validators: list = []


def register_transfer_validator(fn):
    """Register a check that ``transfer_ownership`` runs before it changes anything.

    Validators run in registration order inside the transaction, after the unit row is
    locked, and block by raising ``ValidationError``. Usable as a decorator.
    """
    if fn not in _transfer_validators:
        _transfer_validators.append(fn)
    return fn


def unregister_transfer_validator(fn) -> None:
    if fn in _transfer_validators:
        _transfer_validators.remove(fn)


def _normalize_owners(owners: Iterable) -> list[tuple[Person, Decimal]]:
    items = list(owners)
    if not items:
        raise ValidationError(_("A unit needs at least one owner."))
    pairs: list[tuple[Person, Decimal | None]] = [
        (o[0], Decimal(o[1])) if isinstance(o, tuple) else (o, None) for o in items
    ]
    if all(p is None for _person, p in pairs):
        # Equal shares; the first owner absorbs the rounding remainder.
        n = len(pairs)
        each = (HUNDRED / n).quantize(Decimal("0.0001"))
        result = [(person, each) for person, _p in pairs]
        result[0] = (result[0][0], HUNDRED - each * (n - 1))
        return result
    if any(p is None for _person, p in pairs):
        raise ValidationError(_("Give a share for every owner, or for none."))
    if sum(p for _person, p in pairs) != HUNDRED:
        raise ValidationError(_("Ownership shares must add up to 100%."))
    ids = [person.pk for person, _p in pairs]
    if len(set(ids)) != len(ids):
        raise ValidationError(_("The same person is listed twice."))
    return pairs  # type: ignore[return-value]


def _create_period(unit, start, owners, notes="") -> OwnershipPeriod:
    period = OwnershipPeriod(association=unit.association, unit=unit, start=start, notes=notes)
    period.full_clean()
    period.save()
    for i, (person, pct) in enumerate(_normalize_owners(owners)):
        if person.association_id != unit.association_id:
            raise ValidationError(_("Owners must belong to the same association."))
        OwnershipShare.objects.create(
            association=unit.association,
            period=period,
            person=person,
            percent=pct,
            is_primary=i == 0,
        )
    return period


@transaction.atomic
def set_initial_ownership(unit: Unit, owners: Owners, start: datetime.date) -> OwnershipPeriod:
    """Record the first known ownership of a unit (setup and imports)."""
    if OwnershipPeriod.all_associations.filter(unit=unit).exists():
        raise ValidationError(_("This unit already has ownership history; use a transfer."))
    return _create_period(unit, start, owners)


@transaction.atomic
def transfer_ownership(
    unit: Unit, owners: Owners, effective_date: datetime.date, notes: str = ""
) -> OwnershipPeriod:
    """Close the current period the day before ``effective_date`` and open a new one.

    History is never rewritten: the effective date must be after the start of the latest
    period.
    """
    unit = Unit.all_associations.select_for_update().get(pk=unit.pk)
    for validator in list(_transfer_validators):
        validator(unit, owners, effective_date)
    latest = OwnershipPeriod.all_associations.filter(unit=unit).order_by("-start").first()
    if latest is not None:
        if effective_date <= latest.start:
            raise ValidationError(
                _("The effective date must be after %(date)s, when the current ownership began.")
                % {"date": latest.start.isoformat()}
            )
        if latest.end is not None and latest.end >= effective_date:
            raise ValidationError(_("The effective date overlaps existing ownership history."))
        if latest.end is None:
            latest.end = effective_date - datetime.timedelta(days=1)
            latest.save(update_fields=["end"])
    period = _create_period(unit, effective_date, owners, notes)
    events.publish(
        OwnershipTransferred(
            association_id=unit.association_id,
            unit_id=unit.pk,
            previous_period_id=latest.pk if latest else None,
            new_period_id=period.pk,
            effective_date=effective_date,
            owner_ids=[s.person_id for s in period.shares.all()],
        )
    )
    return period


@transaction.atomic
def add_occupancy(
    unit: Unit,
    person: Person,
    start: datetime.date,
    kind: str = Occupancy.Kind.TENANT,
    end: datetime.date | None = None,
    **extra,
) -> Occupancy:
    occ = Occupancy(
        association=unit.association,
        unit=unit,
        person=person,
        kind=kind,
        start=start,
        end=end,
        **extra,
    )
    occ.full_clean()
    occ.save()
    return occ


CONTACT_FIELDS = (
    "preferred_name",
    "email",
    "alt_email",
    "phone",
    "alt_phone",
    "address_line1",
    "address_line2",
    "city",
    "region",
    "postal_code",
    # Owners keep their phone or email out of the records export and the directory.
    "phone_unlisted",
    "email_confidential",
)
CONTACT_FLAGS = frozenset({"phone_unlisted", "email_confidential"})


@transaction.atomic
def update_contact(person: Person, **fields) -> Person:
    """Change a person's contact details (self-service in the portal). Audited via the model."""
    unknown = set(fields) - set(CONTACT_FIELDS)
    if unknown:
        raise ValueError(f"not contact fields: {sorted(unknown)}")
    for name, value in fields.items():
        if name in CONTACT_FLAGS:
            setattr(person, name, bool(value))
        else:
            setattr(person, name, (value or "").strip())
    person.full_clean()
    person.save()
    return person


@transaction.atomic
def end_occupancy(occupancy: Occupancy, end: datetime.date | None = None) -> Occupancy:
    """Close an occupancy (someone moved out). Never deletes, so history is kept."""
    end = end or association_today(occupancy.association)
    if occupancy.end is None or occupancy.end > end:
        occupancy.end = max(end, occupancy.start)
        occupancy.full_clean()
        occupancy.save()
    return occupancy


@transaction.atomic
def record_notice_preference(
    person: Person,
    delivery: str,
    *,
    e_delivery_consent: bool | None = None,
    source: str = NoticePreference.Source.BOARD,
    recorded_by=None,
    note: str = "",
    consent_text_version: str = "",
    evidence_key: str = "",
    evidence_name: str = "",
    revoke_reason: str = "",
) -> NoticePreference:
    """Append a consent record. Email delivery always implies e-delivery consent.

    Electronic-delivery consent is only accepted from the owner in HomeOA (portal or
    invitation) or from a signed paper form with its scan; an import never consents. A
    record that ends an earlier consent gets ``revoked_at`` and ``revoke_reason``.
    """
    if e_delivery_consent is None:
        e_delivery_consent = delivery == NoticePreference.Delivery.EMAIL
    if e_delivery_consent:
        if source not in NoticePreference.CONSENT_SOURCES:
            raise ValidationError(
                _(
                    "Consent to electronic notice must come from the owner in HomeOA or a "
                    "signed paper form."
                )
            )
        if source == NoticePreference.Source.PAPER and not evidence_key:
            raise ValidationError(_("Upload the scan of the signed consent form."))
        if not person.email:
            raise ValidationError(_("Add an email address before choosing email notices."))
    elif delivery == NoticePreference.Delivery.EMAIL:
        raise ValidationError(_("Email notices need consent to electronic delivery."))
    previous = current_notice_preference(person)
    if not revoke_reason and not e_delivery_consent and previous and previous.valid_consent:
        revoke_reason = (
            NoticePreference.RevokeReason.OWNER
            if source in (NoticePreference.Source.PORTAL, NoticePreference.Source.INVITE)
            else NoticePreference.RevokeReason.BOARD
        )
    pref = NoticePreference(
        association=person.association,
        person=person,
        delivery=delivery,
        e_delivery_consent=e_delivery_consent,
        email=person.email if e_delivery_consent else "",
        source=source,
        recorded_by=recorded_by if getattr(recorded_by, "is_authenticated", False) else None,
        note=note,
        consent_text_version=consent_text_version if e_delivery_consent else "",
        evidence_key=evidence_key,
        evidence_name=evidence_name,
        revoked_at=timezone.now() if revoke_reason else None,
        revoke_reason=revoke_reason,
    )
    pref.full_clean()
    pref.save()
    audit.record(
        "notice_preference.consent_given"
        if e_delivery_consent
        else (
            "notice_preference.consent_revoked" if revoke_reason else "notice_preference.recorded"
        ),
        pref,
        actor=pref.recorded_by,
        changes={
            "delivery": delivery,
            "source": source,
            "email": pref.email,
            "consent_text_version": pref.consent_text_version,
            "revoke_reason": revoke_reason,
        },
    )
    events.publish(
        NoticePreferenceChanged(
            association_id=person.association_id,
            person_id=person.pk,
            preference_id=pref.pk,
            delivery=delivery,
            e_delivery_consent=e_delivery_consent,
        )
    )
    return pref


def revoke_consent(
    person: Person, reason: str, *, by=None, note: str = ""
) -> NoticePreference | None:
    """End the person's electronic-delivery consent (falls back to mail). None if there was
    no consent to end."""
    current = current_notice_preference(person)
    if current is None or not current.e_delivery_consent:
        return None
    return record_notice_preference(
        person,
        NoticePreference.Delivery.POSTAL,
        e_delivery_consent=False,
        source=NoticePreference.Source.SYSTEM
        if reason in (NoticePreference.RevokeReason.FAILURES, NoticePreference.RevokeReason.IMPORT)
        else NoticePreference.Source.BOARD,
        recorded_by=by,
        note=note,
        revoke_reason=reason,
    )


def find_or_create_person(association, full_name: str, email: str = "") -> Person:
    """Reuse a person with the same email in this association, else create one."""
    email = email.strip()
    if email:
        existing = (
            Person.all_associations.filter(association=association, email__iexact=email)
            .order_by("pk")
            .first()
        )
        if existing:
            return existing
    return Person.all_associations.create(
        association=association, full_name=full_name.strip(), email=email
    )


# ---------------------------------------------------------------- lease files (FileStorage)

LEASE_FOLDER = "registry/leases"


def store_lease(unit: Unit, upload) -> str:
    """Store an uploaded lease through the FileStorage provider; returns its key ("" if none)."""
    if not upload:
        return ""
    from homeoa import providers

    storage = providers.get_provider("FileStorage", unit.association)
    stored = storage.put(
        unit.association_id,
        getattr(upload, "name", "") or "lease",
        upload,
        content_type=getattr(upload, "content_type", "") or "application/octet-stream",
        folder=LEASE_FOLDER,
    )
    return stored.key


def lease_download_url(occupancy: Occupancy) -> str:
    """A short-lived signed URL for the lease. Only permission-checked views may call this."""
    from homeoa import providers

    storage = providers.get_provider("FileStorage", occupancy.association)
    name = occupancy.lease_key.rsplit("/", 1)[-1].split("-", 1)[-1]
    return storage.signed_url(
        occupancy.lease_key, association_id=occupancy.association_id, filename=name
    )
