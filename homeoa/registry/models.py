"""Registry: units, people, ownership periods, occupancy and notice consent (REG-01..04, 06).

Ownership is modelled as dated periods. Each :class:`OwnershipPeriod` covers one unit from
``start`` to ``end`` (both inclusive; ``end`` is null while current) and holds one or more
:class:`OwnershipShare` rows, one per owner. Owner ledgers hang off
``unit`` + ``ownership_period``. Use :mod:`homeoa.registry.services` to change ownership;
never edit periods by hand.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class AddressFields(models.Model):
    """Postal address columns shared by units and people."""

    address_line1 = models.CharField(_("address line 1"), max_length=200, blank=True)
    address_line2 = models.CharField(_("address line 2"), max_length=200, blank=True)
    city = models.CharField(_("city"), max_length=100, blank=True)
    region = models.CharField(_("state"), max_length=50, blank=True)
    postal_code = models.CharField(_("ZIP code"), max_length=20, blank=True)
    country = models.CharField(_("country"), max_length=2, default="US", blank=True)

    class Meta:
        abstract = True

    @property
    def address_lines(self) -> list[str]:
        city_line = " ".join(
            p for p in [f"{self.city}," if self.city else "", self.region, self.postal_code] if p
        ).strip(", ")
        return [line for line in [self.address_line1, self.address_line2, city_line] if line]


class Unit(AddressFields, AssociationScopedModel):
    number = models.CharField(_("unit number"), max_length=20)
    building = models.CharField(_("building"), max_length=50, blank=True)
    parcel_id = models.CharField(_("parcel ID"), max_length=50, blank=True)
    allocated_interest = models.DecimalField(
        _("allocated interest (%)"),
        max_digits=7,
        decimal_places=4,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    vote_weight = models.DecimalField(
        _("custom vote weight"),
        max_digits=9,
        decimal_places=4,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("Used only when the association weighs votes by a custom value."),
    )
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        verbose_name = _("unit")
        verbose_name_plural = _("units")
        constraints = [
            models.UniqueConstraint(
                fields=["association", "number"], name="registry_unit_number_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(allocated_interest__gte=0, allocated_interest__lte=100),
                name="registry_unit_interest_range",
            ),
            models.CheckConstraint(
                condition=models.Q(vote_weight__gte=0), name="registry_unit_vote_weight_gte_0"
            ),
        ]

    def __str__(self) -> str:
        return str(_("Unit %(number)s") % {"number": self.number})

    @property
    def sort_key(self):
        return (0, int(self.number), "") if self.number.isdigit() else (1, 0, self.number)


ENGLISH = "en"

# Owner preferred languages. Codes are BCP 47 / Django language codes. The list is
# the languages most spoken at home in Washington plus a few more; extend as needed.
PREFERRED_LANGUAGES = [
    (ENGLISH, _("English")),
    ("es", _("Spanish")),
    ("zh-hans", _("Chinese (Simplified)")),
    ("zh-hant", _("Chinese (Traditional)")),
    ("vi", _("Vietnamese")),
    ("ko", _("Korean")),
    ("ru", _("Russian")),
    ("uk", _("Ukrainian")),
    ("tl", _("Tagalog")),
    ("so", _("Somali")),
    ("am", _("Amharic")),
    ("ti", _("Tigrinya")),
    ("ar", _("Arabic")),
    ("fa", _("Persian")),
    ("hi", _("Hindi")),
    ("pa", _("Punjabi")),
    ("ja", _("Japanese")),
    ("km", _("Khmer")),
    ("pt", _("Portuguese")),
    ("fr", _("French")),
    ("de", _("German")),
]


def language_label(code: str) -> str:
    return str(dict(PREFERRED_LANGUAGES).get(code, code))


class Person(AddressFields, AssociationScopedModel):
    """An owner, tenant or contact. Separate from login users; may link to one."""

    full_name = models.CharField(_("full name"), max_length=200)
    preferred_name = models.CharField(_("preferred name"), max_length=100, blank=True)
    email = models.EmailField(_("email"), blank=True)
    alt_email = models.EmailField(_("other email"), blank=True)
    phone = models.CharField(_("phone"), max_length=40, blank=True)
    alt_phone = models.CharField(_("other phone"), max_length=40, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="registry_people",
        verbose_name=_("login user"),
    )
    preferred_language = models.CharField(
        _("preferred language"),
        max_length=10,
        choices=PREFERRED_LANGUAGES,
        default=ENGLISH,
        help_text=_(
            "Statutory notices go out in English plus this language. Another language "
            "holds the notice until the board uploads an approved translation."
        ),
    )
    # Records-export and directory privacy. Owners set the phone and email flags
    # themselves; the board sets the Address Confidentiality Program (ACP) flag.
    phone_unlisted = models.BooleanField(
        _("unlisted phone"),
        default=False,
        help_text=_("Left out of the owner records export and the resident directory."),
    )
    email_confidential = models.BooleanField(
        _("keep email confidential"),
        default=False,
        help_text=_("Left out of the owner records export and the resident directory."),
    )
    acp_participant = models.BooleanField(
        _("Address Confidentiality Program participant"),
        default=False,
        help_text=_("Their address is left out of the owner export and the directory."),
    )
    acp_address = models.TextField(
        _("ACP substitute address"),
        blank=True,
        help_text=_("The address the Address Confidentiality Program assigned. Optional."),
    )
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        verbose_name = _("person")
        verbose_name_plural = _("people")
        ordering = ["full_name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "user"],
                condition=models.Q(user__isnull=False),
                name="registry_person_user_unique",
            )
        ]

    def __str__(self) -> str:
        return self.full_name

    @property
    def initials(self) -> str:
        parts = [p for p in self.full_name.replace("&", " ").split() if p[:1].isalpha()]
        if not parts:
            return "?"
        return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


def _check_dates(start, end):
    if start and end and end < start:
        raise ValidationError({"end": _("The end date must be on or after the start date.")})


class OwnershipPeriod(AssociationScopedModel):
    """One stretch of ownership of a unit. Periods of a unit never overlap."""

    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT, related_name="ownership_periods", verbose_name=_("unit")
    )
    start = models.DateField(_("start date"))
    end = models.DateField(_("end date"), null=True, blank=True)
    owners = models.ManyToManyField(
        Person, through="OwnershipShare", related_name="ownership_periods"
    )
    notes = models.CharField(_("notes"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("ownership period")
        verbose_name_plural = _("ownership periods")
        ordering = ["unit", "-start"]
        constraints = [
            models.UniqueConstraint(fields=["unit", "start"], name="registry_period_unit_start"),
            models.UniqueConstraint(
                fields=["unit"],
                condition=models.Q(end__isnull=True),
                name="registry_period_one_open_per_unit",
            ),
            models.CheckConstraint(
                condition=models.Q(end__isnull=True) | models.Q(end__gte=models.F("start")),
                name="registry_period_end_after_start",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.unit} {self.start}–{self.end or '…'}"

    def covers(self, on: datetime.date) -> bool:
        return self.start <= on and (self.end is None or on <= self.end)

    def clean(self):
        _check_dates(self.start, self.end)
        if self.unit_id and self.start:
            overlapping = OwnershipPeriod.all_associations.filter(unit_id=self.unit_id).exclude(
                pk=self.pk
            )
            far = datetime.date.max
            for other in overlapping:
                if self.start <= (other.end or far) and other.start <= (self.end or far):
                    raise ValidationError(_("Ownership periods of a unit cannot overlap."))


class OwnershipShare(AssociationScopedModel):
    period = models.ForeignKey(
        OwnershipPeriod,
        on_delete=models.CASCADE,
        related_name="shares",
        verbose_name=_("ownership period"),
    )
    person = models.ForeignKey(
        Person, on_delete=models.PROTECT, related_name="ownership_shares", verbose_name=_("owner")
    )
    percent = models.DecimalField(
        _("ownership share (%)"), max_digits=7, decimal_places=4, default=Decimal("100")
    )
    is_primary = models.BooleanField(_("primary contact"), default=False)

    class Meta:
        verbose_name = _("ownership share")
        verbose_name_plural = _("ownership shares")
        ordering = ["-is_primary", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["period", "person"], name="registry_share_unique"),
            models.CheckConstraint(
                condition=models.Q(percent__gt=0, percent__lte=100),
                name="registry_share_percent_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.person} · {self.period}"


class Occupancy(AssociationScopedModel):
    """Someone living in a unit who is not (necessarily) an owner: tenants, residents."""

    class Kind(models.TextChoices):
        TENANT = "tenant", _("Tenant")
        RESIDENT = "resident", _("Other resident")

    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT, related_name="occupancies", verbose_name=_("unit")
    )
    person = models.ForeignKey(
        Person, on_delete=models.PROTECT, related_name="occupancies", verbose_name=_("person")
    )
    kind = models.CharField(_("kind"), max_length=20, choices=Kind, default=Kind.TENANT)
    start = models.DateField(_("start date"))
    end = models.DateField(_("end date"), null=True, blank=True)
    # FileStorage key of the lease (providers.storage); download via registry:lease.
    lease_key = models.CharField(_("lease"), max_length=300, blank=True)
    notes = models.CharField(_("notes"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("occupancy")
        verbose_name_plural = _("occupancies")
        ordering = ["unit", "-start"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end__isnull=True) | models.Q(end__gte=models.F("start")),
                name="registry_occupancy_end_after_start",
            )
        ]

    def __str__(self) -> str:
        return f"{self.person} · {self.unit}"

    def clean(self):
        _check_dates(self.start, self.end)

    def covers(self, on: datetime.date) -> bool:
        return self.start <= on and (self.end is None or on <= self.end)


class ImmutableRecordError(Exception):
    pass


class NoticePreference(AssociationScopedModel):
    """An append-only consent record: how a person agreed to get notices, when and why.

    The newest record per person is their current preference. Records are never edited;
    a change is a new record, so the history is kept (REG-04).
    """

    class Delivery(models.TextChoices):
        EMAIL = "email", _("Email")
        POSTAL = "postal", _("Postal mail")

    class Source(models.TextChoices):
        BOARD = "board", _("Entered by the board")
        PAPER = "paper", _("Signed paper form")
        PORTAL = "portal", _("Resident portal")
        INVITE = "invite", _("Invitation acceptance")
        SYSTEM = "system", _("Automatic")
        IMPORT = "import", _("Import")

    class RevokeReason(models.TextChoices):
        OWNER = "owner", _("Withdrawn by the owner")
        FAILURES = "failures", _("Two consecutive failed email deliveries")
        IMPORT = "import", _("Imported consent is not valid consent")
        BOARD = "board", _("Changed by the board")

    #: Sources whose consent counts for electronic notice. Imports never do.
    CONSENT_SOURCES = ("portal", "invite", "paper")

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="notice_preferences")
    delivery = models.CharField(_("notices by"), max_length=20, choices=Delivery)
    e_delivery_consent = models.BooleanField(_("consents to electronic delivery"), default=False)
    email = models.EmailField(_("email used"), blank=True)
    source = models.CharField(_("source"), max_length=20, choices=Source, default=Source.BOARD)
    recorded_at = models.DateTimeField(_("recorded at"), auto_now_add=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("recorded by"),
    )
    note = models.CharField(_("note"), max_length=200, blank=True)
    consent_text_version = models.CharField(_("consent text version"), max_length=40, blank=True)
    evidence_key = models.CharField(
        _("signed form (file key)"),
        max_length=300,
        blank=True,
        help_text=_("FileStorage key of the scanned paper consent."),
    )
    evidence_name = models.CharField(_("signed form file name"), max_length=200, blank=True)
    revoked_at = models.DateTimeField(
        _("revoked at"),
        null=True,
        blank=True,
        help_text=_("Set when this record revokes an earlier electronic delivery consent."),
    )
    revoke_reason = models.CharField(
        _("revocation reason"), max_length=20, choices=RevokeReason, blank=True
    )

    class Meta:
        verbose_name = _("notice preference")
        verbose_name_plural = _("notice preferences")
        ordering = ["-recorded_at", "-pk"]
        get_latest_by = ["recorded_at", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(delivery="email") | models.Q(e_delivery_consent=True),
                name="registry_notice_email_needs_consent",
            )
        ]

    def __str__(self) -> str:
        return f"{self.person} · {self.get_delivery_display()}"

    @property
    def valid_consent(self) -> bool:
        """Consent that allows legal notices by email: given in HomeOA or on a
        signed paper form, naming an address."""
        return (
            self.e_delivery_consent
            and self.source in self.CONSENT_SOURCES
            and bool(self.email)
            and self.revoked_at is None
        )

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise ImmutableRecordError("Notice preferences are append-only; record a new one.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError("Notice preferences are append-only.")


class RecordsRequest(AssociationScopedModel):
    """An owner's request to inspect or copy association records.

    ``due_on`` and ``latest_on`` come from the rule pack when the request is logged
    (``records.inspection_days`` / ``records.inspection_max_days``); unset rules leave them
    blank.
    """

    person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="records_requests",
        verbose_name=_("requested by"),
    )
    requester_name = models.CharField(_("requester name"), max_length=200, blank=True)

    class Delivery(models.TextChoices):
        COPIES = "copies", _("Copies by email")
        INSPECT = "inspect", _("Inspect in person")

    delivery = models.CharField(
        _("how the owner wants them"), max_length=10, choices=Delivery, blank=True
    )
    received_on = models.DateField(_("received on"))
    due_on = models.DateField(_("records due"), null=True, blank=True)
    latest_on = models.DateField(_("latest extended due date"), null=True, blank=True)
    fulfilled_on = models.DateField(_("fulfilled on"), null=True, blank=True)
    note = models.CharField(_("note"), max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("records request")
        verbose_name_plural = _("records requests")
        ordering = ["-received_on", "-pk"]

    def __str__(self) -> str:
        who = self.person.full_name if self.person_id else self.requester_name
        return str(_("Records request from %(who)s") % {"who": who or "?"})
