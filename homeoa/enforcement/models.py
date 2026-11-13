"""Violation categories, fine schedules and the violation log (VIO-01, VIO-02).

Fines themselves (hearings, posting to the owner ledger) are in ``hearings``. A fine schedule
records when it was adopted and when it was furnished to owners; ``hearings`` refuses any fine
unless the schedule in force on the violation date was furnished on or before that date
.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import MoneyField
from homeoa.core.models import AssociationScopedModel


class ViolationCategory(AssociationScopedModel):
    name = models.CharField(_("name"), max_length=120)
    rule_citation = models.CharField(_("rule citation"), max_length=300)
    governing_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("governing document version"),
    )
    active = models.BooleanField(_("active"), default=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("violation category")
        verbose_name_plural = _("violation categories")
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "name"], name="enforcement_category_name_unique"
            )
        ]

    def __str__(self) -> str:
        return self.name


class FineSchedule(AssociationScopedModel):
    """One version of the fine schedule. The version in force on a date is the one with
    the latest ``effective_on`` not after it."""

    version = models.PositiveIntegerField(_("version"))
    effective_on = models.DateField(_("effective date"))
    adopted_on = models.DateField(_("adopted on"))
    furnished_on = models.DateField(_("furnished to owners on"), null=True, blank=True)
    resolution = models.ForeignKey(
        "governance.Resolution",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("resolution"),
    )
    note = models.CharField(_("note"), max_length=300, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("fine schedule")
        verbose_name_plural = _("fine schedules")
        ordering = ["-effective_on", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "version"], name="enforcement_schedule_version_unique"
            )
        ]

    def __str__(self) -> str:
        return str(_("Fine schedule v%(n)s") % {"n": self.version})

    @property
    def is_furnished(self) -> bool:
        return self.furnished_on is not None

    def furnished_by(self, on) -> bool:
        """Was this schedule furnished to owners on or before ``on``?"""
        return self.furnished_on is not None and self.furnished_on <= on

    def clean(self):
        if self.adopted_on and self.effective_on and self.effective_on < self.adopted_on:
            raise ValidationError(_("The effective date cannot be before the adoption date."))
        if self.furnished_on and self.adopted_on and self.furnished_on < self.adopted_on:
            raise ValidationError(_("A schedule cannot be furnished before it is adopted."))


class FineScheduleLine(AssociationScopedModel):
    schedule = models.ForeignKey(
        FineSchedule, on_delete=models.CASCADE, related_name="lines", verbose_name=_("schedule")
    )
    category = models.ForeignKey(
        ViolationCategory,
        on_delete=models.PROTECT,
        related_name="schedule_lines",
        verbose_name=_("category"),
    )
    first_amount = MoneyField(_("first offense"))
    repeat_amount = MoneyField(_("repeat offense"))
    continuing_amount = MoneyField(_("continuing violation"), null=True, blank=True)
    continuing_period_days = models.PositiveIntegerField(
        _("continuing period (days)"), null=True, blank=True
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("fine schedule line")
        verbose_name_plural = _("fine schedule lines")
        ordering = ["category__name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "category"], name="enforcement_line_category_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.schedule} - {self.category}"

    def clean(self):
        for f in ("first_amount", "repeat_amount", "continuing_amount"):
            v = getattr(self, f)
            if v is not None and v < 0:
                raise ValidationError(_("Fine amounts cannot be negative."))
        if (self.continuing_amount is None) != (self.continuing_period_days is None):
            raise ValidationError(_("A continuing fine needs both an amount and a period in days."))
        if self.continuing_period_days == 0:
            raise ValidationError(_("The continuing period must be at least one day."))


class Violation(AssociationScopedModel):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        CURED = "cured", _("Cured")
        CLOSED = "closed", _("Closed")
        DISMISSED = "dismissed", _("Dismissed")

    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="violations", verbose_name=_("unit")
    )
    category = models.ForeignKey(
        ViolationCategory,
        on_delete=models.PROTECT,
        related_name="violations",
        verbose_name=_("category"),
    )
    observed_on = models.DateField(_("observed on"))
    description = models.TextField(_("description"))
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.OPEN)
    status_note = models.CharField(_("status note"), max_length=300, blank=True)
    cured_on = models.DateField(_("cured on"), null=True, blank=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("reported by"),
    )
    created_at = models.DateTimeField(_("logged"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("violation")
        verbose_name_plural = _("violations")
        ordering = ["-observed_on", "-pk"]

    def __str__(self) -> str:
        return f"{self.unit} - {self.category} ({self.observed_on})"


class ViolationPhoto(AssociationScopedModel):
    """A photo filed in the document library (board-only) and linked to a violation."""

    violation = models.ForeignKey(
        Violation, on_delete=models.PROTECT, related_name="photos", verbose_name=_("violation")
    )
    document = models.ForeignKey(
        "documents.Document",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("document"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("violation photo")
        verbose_name_plural = _("violation photos")
        ordering = ["pk"]

    def __str__(self) -> str:
        return str(self.document)


class ViolationStep(AssociationScopedModel):
    """One rung of the escalation ladder (VIO-03): a notice sent, a hearing notice or a
    decision. Hearings and decisions are recorded by ``hearings``."""

    class Kind(models.TextChoices):
        COURTESY = "courtesy", _("Courtesy notice")
        FORMAL = "formal", _("Formal notice")
        HEARING_NOTICE = "hearing_notice", _("Hearing notice")
        HEARING_CANCELLED = "hearing_cancelled", _("Hearing cancelled")
        DECISION = "decision", _("Decision")

    class Delivery(models.TextChoices):
        EMAIL = "email", _("Email")
        LETTER = "letter", _("Letter")

    violation = models.ForeignKey(
        Violation, on_delete=models.PROTECT, related_name="steps", verbose_name=_("violation")
    )
    kind = models.CharField(_("kind"), max_length=20, choices=Kind)
    sent_on = models.DateField(_("sent on"))
    cure_by = models.DateField(_("cure by"), null=True, blank=True)
    delivery = models.CharField(_("delivery"), max_length=10, choices=Delivery)
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
    emailed = models.PositiveIntegerField(_("email copies"), default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("sent by"),
    )
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("violation step")
        verbose_name_plural = _("violation steps")
        ordering = ["sent_on", "pk"]

    def __str__(self) -> str:
        return f"{self.violation} - {self.get_kind_display()} ({self.sent_on})"


class Hearing(AssociationScopedModel):
    """A hearing on a violation (VIO-04) and the board's decision (VIO-05).

    The hearing notice is a :class:`ViolationStep` (``HEARING_NOTICE``) sent at least
    ``enforcement.hearing_notice_min_days`` before the hearing; the decision letter is a
    ``DECISION`` step. A fine posts as a ``FINE`` charge on the owner ledger (``charge``).
    """

    class Outcome(models.TextChoices):
        DISMISSED = "dismissed", _("Dismissed")
        CURED = "cured", _("Cured, no fine")
        FINE = "fine", _("Fine")

    violation = models.ForeignKey(
        Violation, on_delete=models.PROTECT, related_name="hearings", verbose_name=_("violation")
    )
    hearing_on = models.DateField(_("hearing date"))
    hearing_time = models.TimeField(_("hearing time"), null=True, blank=True)
    place = models.CharField(_("place or online link"), max_length=300)
    online = models.BooleanField(_("online"), default=False)
    notice_step = models.ForeignKey(
        ViolationStep,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("hearing notice"),
    )
    attended = models.BooleanField(_("owner attended"), null=True, blank=True)
    owner_statement = models.TextField(_("owner statement"), blank=True)
    outcome = models.CharField(_("decision"), max_length=10, choices=Outcome, blank=True)
    decision_note = models.TextField(_("decision note"), blank=True)
    decided_on = models.DateField(_("decided on"), null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("decided by"),
    )
    schedule_amount = MoneyField(_("schedule amount"), null=True, blank=True)
    fine_amount = MoneyField(_("fine amount"), null=True, blank=True)
    amount_reason = models.CharField(_("reason for amount change"), max_length=300, blank=True)
    charge = models.ForeignKey(
        "billing.Charge",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("fine charge"),
    )
    decision_step = models.ForeignKey(
        ViolationStep,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("decision letter"),
    )
    cancelled_on = models.DateField(_("cancelled on"), null=True, blank=True)
    cancel_reason = models.CharField(_("reason for cancelling"), max_length=300, blank=True)
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("hearing")
        verbose_name_plural = _("hearings")
        ordering = ["-hearing_on", "-pk"]

    def __str__(self) -> str:
        return str(_("Hearing on %(date)s") % {"date": self.hearing_on.isoformat()})

    @property
    def cancelled(self) -> bool:
        return self.cancelled_on is not None

    @property
    def decided(self) -> bool:
        return bool(self.outcome)

    @property
    def fine_void(self) -> bool:
        return self.charge_id is not None and self.charge.void_entry_id is not None


class ViolationResponse(AssociationScopedModel):
    """An owner's or tenant's answer to a violation (VIO-06): "I fixed it" with proof, or
    "I disagree" with a statement for the hearing record. Nothing changes the violation
    by itself; the board decides."""

    class Kind(models.TextChoices):
        FIXED = "fixed", _("I fixed it")
        DISAGREE = "disagree", _("I disagree")

    violation = models.ForeignKey(
        Violation, on_delete=models.PROTECT, related_name="responses", verbose_name=_("violation")
    )
    kind = models.CharField(_("response"), max_length=10, choices=Kind)
    message = models.TextField(_("message"))
    person = models.ForeignKey(
        "registry.Person",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("from"),
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(_("sent"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("violation response")
        verbose_name_plural = _("violation responses")
        ordering = ["created_at", "pk"]

    def __str__(self) -> str:
        return f"{self.violation} - {self.get_kind_display()}"


class ResponsePhoto(AssociationScopedModel):
    """A photo sent with a response, filed board-only in the document library."""

    response = models.ForeignKey(ViolationResponse, on_delete=models.PROTECT, related_name="photos")
    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT, related_name="+")

    class Meta(AssociationScopedModel.Meta):
        ordering = ["pk"]
