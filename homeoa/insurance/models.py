"""The association's master insurance policy (INS-01), one row per policy term.

The current term's end date drives the compliance calendar's renewal reminder. Owners see
a summary (carrier, term, deductible) on their Docs tab to size their own HO-6 policy.
"""

from __future__ import annotations

import datetime as dt

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import MoneyField
from homeoa.core.models import AssociationScopedModel


class MasterPolicy(AssociationScopedModel):
    carrier = models.CharField(_("insurance company"), max_length=200)
    policy_number = models.CharField(_("policy number"), max_length=100, blank=True)
    start_date = models.DateField(_("term start"))
    end_date = models.DateField(_("term end"))
    deductible = MoneyField(_("deductible"), null=True, blank=True)
    property_limit = MoneyField(_("property coverage limit"), null=True, blank=True)
    liability_limit = MoneyField(_("liability limit"), null=True, blank=True)
    premium = MoneyField(_("yearly premium"), null=True, blank=True)
    agent_name = models.CharField(_("agent or broker"), max_length=200, blank=True)
    agent_email = models.EmailField(_("agent email"), blank=True)
    agent_phone = models.CharField(_("agent phone"), max_length=40, blank=True)
    certificate = models.ForeignKey(
        "documents.Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("certificate or policy document"),
        help_text=_("Owners can open it only if the document is visible to owners."),
    )
    notes = models.TextField(_("notes"), blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("master insurance policy")
        verbose_name_plural = _("master insurance policies")
        ordering = ["-end_date", "-pk"]

    def __str__(self) -> str:
        return f"{self.carrier} {self.start_date}–{self.end_date}"

    def clean(self):
        if self.start_date and self.end_date and self.end_date <= self.start_date:
            raise ValidationError({"end_date": _("The term must end after it starts.")})

    def covers(self, on: dt.date) -> bool:
        return self.start_date <= on < self.end_date
