"""Resale certificate requests (RES-02): one per request, from the seller in the portal or
logged by the board for an agent or escrow. The same request also covers a statement of
unpaid assessments for an owner or a lender (WA: RCW 64.34.364(15))."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import MoneyField
from homeoa.core.models import AssociationScopedModel


class ResaleRequest(AssociationScopedModel):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        ISSUED = "issued", _("Issued")
        CANCELLED = "cancelled", _("Cancelled")

    class Kind(models.TextChoices):
        CERTIFICATE = "certificate", _("Resale certificate")
        STATEMENT = "statement", _("Statement of unpaid assessments")

    kind = models.CharField(_("document"), max_length=12, choices=Kind, default=Kind.CERTIFICATE)

    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    seller = models.ForeignKey(
        "registry.Person",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("selling owner"),
    )
    requester_name = models.CharField(_("requested by"), max_length=200, blank=True)
    send_to = models.EmailField(_("send the certificate to"))
    received_on = models.DateField(_("received on"))
    due_on = models.DateField(_("due"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.OPEN)
    form_id = models.CharField(_("certificate form"), max_length=40, blank=True)
    # {item key: answer text}; pre-filled when the request is opened, edited by the board.
    answers = models.JSONField(default=dict, blank=True)
    fee = MoneyField(_("fee"), null=True, blank=True)
    charge = models.ForeignKey(
        "billing.Charge",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("fee charge"),
    )
    issued_on = models.DateField(_("issued on"), null=True, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    pdf_key = models.CharField(max_length=300, blank=True)
    note = models.CharField(_("note"), max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("resale certificate request")
        verbose_name_plural = _("resale certificate requests")
        ordering = ["-received_on", "-pk"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}, {self.unit}"

    @property
    def is_statement(self) -> bool:
        return self.kind == self.Kind.STATEMENT
