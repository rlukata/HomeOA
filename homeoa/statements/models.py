"""Delinquency ladder state (FIN-17). Owner statements add their models here."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class Step(models.TextChoices):
    REMINDER = "reminder", _("Payment reminder")
    LATE_NOTICE = "late_notice", _("Late notice")
    DEMAND = "demand", _("Demand letter")
    FIRST_NOTICE = "first_notice", _("First notice of delinquency")
    SECOND_NOTICE = "second_notice", _("Second notice of delinquency")


class DelinquencyCase(AssociationScopedModel):
    """One run of the ladder for a unit: opens when a charge is past due, closes when paid."""

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        CLOSED = "closed", _("Closed")

    unit = models.ForeignKey(
        "registry.Unit",
        on_delete=models.PROTECT,
        related_name="delinquency_cases",
        verbose_name=_("unit"),
    )
    anchor_due_date = models.DateField(
        _("oldest due date"),
        help_text=_(
            "Due date of the oldest unpaid charge, or the go-live date if later; step offsets "
            "count from it."
        ),
    )
    opened_on = models.DateField(_("opened on"))
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.OPEN)
    closed_on = models.DateField(_("closed on"), null=True, blank=True)
    closed_reason = models.CharField(_("closed because"), max_length=200, blank=True)
    on_hold = models.BooleanField(_("on hold"), default=False)
    hold_reason = models.CharField(_("hold reason"), max_length=300, blank=True)
    threshold_reached_on = models.DateField(
        _("foreclosure threshold reached on"),
        null=True,
        blank=True,
        help_text=_("First day the owed assessments reached the rule-pack threshold."),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("delinquency case")
        constraints = [
            models.UniqueConstraint(
                fields=["association", "unit"],
                condition=models.Q(status="open"),
                name="statements_one_open_case_per_unit",
            )
        ]

    def __str__(self) -> str:
        return f"{self.unit.number} · {self.anchor_due_date}"


class DelinquencyAction(AssociationScopedModel):
    """A ladder step taken (letter generated) or skipped by the board; once per case."""

    class Outcome(models.TextChoices):
        DONE = "done", _("Done")
        SKIPPED = "skipped", _("Skipped")

    case = models.ForeignKey(
        DelinquencyCase, on_delete=models.CASCADE, related_name="actions", verbose_name=_("case")
    )
    step = models.CharField(_("step"), max_length=20, choices=Step)
    outcome = models.CharField(_("outcome"), max_length=10, choices=Outcome, default=Outcome.DONE)
    as_of = models.DateField(_("date"))
    letter = models.ForeignKey(
        "letters.Letter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("letter"),
    )
    batch = models.ForeignKey(
        "letters.MailBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("mail batch"),
    )
    reason = models.CharField(_("reason"), max_length=300, blank=True)
    statutory_text = models.CharField(_("statutory text"), max_length=60, blank=True)
    contacts_missing = models.BooleanField(_("assistance contacts missing"), default=False)
    reviewed_at = models.DateTimeField(_("reviewed at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("delinquency step")
        ordering = ["as_of", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["case", "step"], name="statements_one_action_per_step")
        ]


class ProtectedRecordError(Exception):
    pass


class PreforeclosureRecord(AssociationScopedModel):
    """One copy of a statutory delinquency notice: a mailed letter or an email.

    Kept as the unit's preforeclosure information (RCW 64.34.364(17)(e), 64.34.372):
    never deleted, and not replaced once its mail batch has left draft.
    """

    class Channel(models.TextChoices):
        MAIL = "mail", _("First-class mail")
        EMAIL = "email", _("Email")

    action = models.ForeignKey(
        DelinquencyAction,
        on_delete=models.PROTECT,
        related_name="records",
        verbose_name=_("notice"),
    )
    channel = models.CharField(_("channel"), max_length=10, choices=Channel)
    letter = models.ForeignKey(
        "letters.Letter",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("letter"),
    )
    address_lines = models.JSONField(_("mailed to"), default=list, blank=True)
    email = models.EmailField(_("emailed to"), blank=True)
    notification = models.ForeignKey(
        "notify.Notification",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("email message"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("preforeclosure record")
        ordering = ["action", "pk"]

    def delete(self, *args, **kwargs):
        raise ProtectedRecordError("Preforeclosure records are kept; they cannot be deleted.")


# Owner statements keep their models in their own module.
from .statement_models import Statement, StatementRun  # noqa: E402, F401
