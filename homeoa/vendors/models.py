"""Vendor directory (VND-01): vendors, contacts, W-9 and remittance details.

The TIN and our account number with the vendor are encrypted at rest
(``core.crypto.EncryptedTextField``) and only ever rendered masked. Revealing the TIN
needs ``finance.manage`` and is audited (``services.reveal_tin``).
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.crypto import EncryptedTextField
from homeoa.core.models import AssociationScopedModel


def _digits(value: str | None) -> str:
    return "".join(c for c in (value or "") if c.isdigit())


def mask_tin(value: str | None, tin_type: str = "ssn") -> str:
    """``***-**-1234`` (SSN) or ``**-***1234`` (EIN); empty when there is no TIN."""
    digits = _digits(value)
    if not digits:
        return ""
    last4 = digits[-4:]
    return f"**-***{last4}" if tin_type == "ein" else f"***-**-{last4}"


def format_tin(value: str | None, tin_type: str = "ssn") -> str:
    digits = _digits(value)
    if len(digits) != 9:
        return value or ""
    if tin_type == "ein":
        return f"{digits[:2]}-{digits[2:]}"
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


def mask_account(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return "••••" + text[-4:] if len(text) > 4 else "••••"


class Vendor(AssociationScopedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        INACTIVE = "inactive", _("Inactive")

    class TaxClassification(models.TextChoices):
        NONE = "", _("Not recorded")
        INDIVIDUAL = "individual", _("Individual / sole proprietor / single-member LLC")
        C_CORP = "c_corp", _("C corporation")
        S_CORP = "s_corp", _("S corporation")
        PARTNERSHIP = "partnership", _("Partnership")
        TRUST = "trust", _("Trust / estate")
        LLC = "llc", _("Limited liability company")
        GOVERNMENT = "government", _("Government entity")
        OTHER = "other", _("Other")

    class TinType(models.TextChoices):
        EIN = "ein", _("EIN")
        SSN = "ssn", _("SSN")

    name = models.CharField(_("name"), max_length=200)
    slug = models.SlugField(_("slug"), max_length=80)
    category = models.CharField(_("category"), max_length=100, blank=True)
    default_expense_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("default expense account"),
    )
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.ACTIVE)
    notes = models.TextField(_("notes"), blank=True)

    # W-9.
    legal_name = models.CharField(_("legal name (W-9)"), max_length=200, blank=True)
    tax_classification = models.CharField(
        _("tax classification"),
        max_length=20,
        choices=TaxClassification,
        default=TaxClassification.NONE,
        blank=True,
    )
    tin_type = models.CharField(_("TIN type"), max_length=3, choices=TinType, default=TinType.EIN)
    tin = EncryptedTextField(_("taxpayer ID number"), null=True, blank=True)
    w9_file_key = models.CharField(_("W-9 file"), max_length=300, blank=True)
    w9_received = models.DateField(_("W-9 received"), null=True, blank=True)
    is_1099_eligible = models.BooleanField(_("1099 eligible"), default=False)

    # Remittance: what the bank's bill pay needs.
    payee_name = models.CharField(_("payee name"), max_length=200, blank=True)
    remit_address_line1 = models.CharField(_("address line 1"), max_length=200, blank=True)
    remit_address_line2 = models.CharField(_("address line 2"), max_length=200, blank=True)
    remit_city = models.CharField(_("city"), max_length=100, blank=True)
    remit_region = models.CharField(_("state"), max_length=50, blank=True)
    remit_postal_code = models.CharField(_("ZIP code"), max_length=20, blank=True)
    account_number = EncryptedTextField(
        _("our account number with the vendor"), null=True, blank=True
    )

    class Meta:
        verbose_name = _("vendor")
        verbose_name_plural = _("vendors")
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "slug"], name="vendors_vendor_slug_unique"
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def tin_masked(self) -> str:
        return mask_tin(self.tin, self.tin_type)

    @property
    def account_number_masked(self) -> str:
        return mask_account(self.account_number)

    @property
    def remit_lines(self) -> list[str]:
        city = ", ".join(p for p in (self.remit_city, self.remit_region) if p)
        last = " ".join(p for p in (city, self.remit_postal_code) if p)
        lines = [self.payee_name or self.legal_name or self.name]
        lines += [p for p in (self.remit_address_line1, self.remit_address_line2, last) if p]
        return lines

    @property
    def primary_contact(self):
        contacts = list(self.contacts.all())
        return next((c for c in contacts if c.is_primary), contacts[0] if contacts else None)


class VendorContact(AssociationScopedModel):
    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="contacts", verbose_name=_("vendor")
    )
    name = models.CharField(_("name"), max_length=200)
    role = models.CharField(_("role"), max_length=100, blank=True)
    email = models.EmailField(_("email"), blank=True)
    phone = models.CharField(_("phone"), max_length=40, blank=True)
    is_primary = models.BooleanField(_("primary contact"), default=False)

    class Meta:
        verbose_name = _("vendor contact")
        verbose_name_plural = _("vendor contacts")
        ordering = ["-is_primary", "name", "pk"]

    def __str__(self) -> str:
        return f"{self.name} ({self.vendor})"


class VendorDocument(AssociationScopedModel):
    """An insurance certificate (COI) or license on file for a vendor (VND-02).

    Files are kept forever: a renewed certificate is a new row; nothing is deleted.
    """

    class Kind(models.TextChoices):
        COI = "coi", _("Insurance certificate")
        LICENSE = "license", _("License")

    vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT, related_name="documents", verbose_name=_("vendor")
    )
    kind = models.CharField(_("kind"), max_length=10, choices=Kind, default=Kind.COI)
    issuer = models.CharField(_("carrier or issuer"), max_length=200, blank=True)
    number = models.CharField(_("policy or license number"), max_length=100, blank=True)
    effective_date = models.DateField(_("effective date"), null=True, blank=True)
    expiry_date = models.DateField(_("expiry date"))
    file_key = models.CharField(_("file"), max_length=300, blank=True)
    file_name = models.CharField(_("file name"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        verbose_name = _("vendor certificate or license")
        verbose_name_plural = _("vendor certificates and licenses")
        ordering = ["vendor", "kind", "-expiry_date", "-pk"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.vendor} · {self.expiry_date}"

    @property
    def is_current(self) -> bool:
        """True unless a later certificate of the same kind replaced it."""
        return not VendorDocument.all_associations.filter(
            vendor_id=self.vendor_id, kind=self.kind, expiry_date__gt=self.expiry_date
        ).exists()


class Contract(AssociationScopedModel):
    """A vendor contract with its term and renewal terms (VND-03). Kept forever."""

    vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT, related_name="contracts", verbose_name=_("vendor")
    )
    title = models.CharField(_("title"), max_length=200)
    start_date = models.DateField(_("term start"))
    end_date = models.DateField(_("term end"))
    auto_renew = models.BooleanField(_("renews automatically"), default=False)
    notice_days = models.PositiveIntegerField(
        _("notice to cancel (days)"),
        default=0,
        help_text=_("Days before the term ends by which the vendor must be told not to renew."),
    )
    amount = models.DecimalField(
        _("amount"), max_digits=12, decimal_places=2, null=True, blank=True
    )
    file_key = models.CharField(_("file"), max_length=300, blank=True)
    file_name = models.CharField(_("file name"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        verbose_name = _("contract")
        verbose_name_plural = _("contracts")
        ordering = ["vendor", "-end_date", "-pk"]

    def __str__(self) -> str:
        return f"{self.title} ({self.vendor})"

    @property
    def notice_date(self) -> dt.date:
        """The last day to tell the vendor not to renew."""
        return self.end_date - dt.timedelta(days=self.notice_days)


class VendorAlert(AssociationScopedModel):
    """A board to-do raised by the daily vendor job; one per document or contract term."""

    class Kind(models.TextChoices):
        DOCUMENT = "document", _("Certificate or license expiring")
        RENEWAL = "renewal", _("Contract renewal decision")

    kind = models.CharField(_("kind"), max_length=10, choices=Kind)
    key = models.CharField(_("key"), max_length=100)
    document = models.ForeignKey(
        VendorDocument, on_delete=models.CASCADE, null=True, blank=True, related_name="alerts"
    )
    contract = models.ForeignKey(
        Contract, on_delete=models.CASCADE, null=True, blank=True, related_name="alerts"
    )
    due = models.DateField(_("due"))
    raised_on = models.DateField(_("raised on"))
    resolved_on = models.DateField(_("resolved on"), null=True, blank=True)

    class Meta:
        verbose_name = _("vendor alert")
        verbose_name_plural = _("vendor alerts")
        ordering = ["due", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["association", "key"], name="vendors_alert_key_unique")
        ]

    def __str__(self) -> str:
        return self.key


class DocumentRequest(AssociationScopedModel):
    """The board emailed the vendor for a new certificate or license (kept)."""

    document = models.ForeignKey(VendorDocument, on_delete=models.PROTECT, related_name="requests")
    contact = models.ForeignKey(
        VendorContact, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    email = models.EmailField(_("sent to"))
    subject = models.CharField(_("subject"), max_length=300)
    body = models.TextField(_("body"))
    sent_at = models.DateTimeField(_("sent at"), auto_now_add=True)
    sent = models.BooleanField(_("sent"), default=False)
    error = models.TextField(_("error"), blank=True)
    message_id = models.CharField(max_length=200, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = _("certificate request")
        verbose_name_plural = _("certificate requests")
        ordering = ["-sent_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.document} → {self.email}"
