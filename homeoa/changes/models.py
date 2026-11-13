"""Change requests: changes that take effect only after board approval.

A proposer asks for a change (a ``kind`` with a JSON ``payload``). If the proposer is on the
board, one other board member must approve; otherwise two board members must. Nobody
approves their own request. When enough approvals are recorded the change is applied by
the kind's ``apply`` function; one rejection closes the request.

Dues changes and special assessments are approved differently: they cite a board
resolution and take effect when it is adopted, instead of on board members' clicks.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class ChangeRequest(AssociationScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", _("Waiting for approval")
        APPLIED = "applied", _("Approved and applied")
        REJECTED = "rejected", _("Rejected")
        WITHDRAWN = "withdrawn", _("Withdrawn")
        FAILED = "failed", _("Approved, but could not be applied")

    kind = models.CharField(_("kind"), max_length=60)
    summary = models.CharField(_("summary"), max_length=300)
    # [[label, value], ...] shown to approvers, in the proposer's words at proposal time.
    details = models.JSONField(_("details"), default=list, blank=True)
    payload = models.JSONField(_("payload"), default=dict, blank=True)
    reason = models.TextField(_("reason"), blank=True)
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("proposed by"),
    )
    proposed_at = models.DateTimeField(_("proposed at"), auto_now_add=True)
    proposer_on_board = models.BooleanField(_("proposer on the board"), default=False)
    required_approvals = models.PositiveSmallIntegerField(_("approvals required"), default=1)
    # Board members who may not approve this one (e.g. the person whose role it changes).
    excluded = models.JSONField(_("may not approve"), default=list, blank=True)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.PENDING
    )
    closed_at = models.DateTimeField(_("closed at"), null=True, blank=True)
    error = models.TextField(_("error"), blank=True)
    # Approval by an adopted board resolution instead of clicks.
    resolution = models.OneToOneField(
        "governance.Resolution",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="change_request",
        verbose_name=_("board resolution"),
    )
    # Where to see the result once applied (e.g. the vendor's page).
    result_url = models.CharField(_("result link"), max_length=300, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("change request")
        verbose_name_plural = _("change requests")
        ordering = ["-proposed_at", "-pk"]

    def __str__(self) -> str:
        return f"#{self.pk} {self.summary}"

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def approvals(self) -> int:
        return self.decisions.filter(decision=ChangeDecision.Decision.APPROVE).count()


class ChangeDecision(AssociationScopedModel):
    class Decision(models.TextChoices):
        APPROVE = "approve", _("Approved")
        REJECT = "reject", _("Rejected")

    change = models.ForeignKey(
        ChangeRequest,
        on_delete=models.CASCADE,
        related_name="decisions",
        verbose_name=_("change request"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("board member"),
    )
    decision = models.CharField(_("decision"), max_length=10, choices=Decision.choices)
    comment = models.TextField(_("comment"), blank=True)
    decided_at = models.DateTimeField(_("decided at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("change decision")
        verbose_name_plural = _("change decisions")
        ordering = ["decided_at", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["change", "user"], name="changes_one_decision_each")
        ]

    def __str__(self) -> str:
        return f"{self.user} {self.get_decision_display()} #{self.change_id}"
