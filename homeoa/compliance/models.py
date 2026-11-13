import calendar
import datetime as dt

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class PeriodStatus(models.TextChoices):
    OPEN = "open", _("Open")
    DONE = "done", _("Done")
    EXEMPT = "exempt", _("Exempt")


class ObligationPeriod(AssociationScopedModel):
    """One period (calendar item) of a rule-pack obligation."""

    obligation_id = models.CharField(_("obligation"), max_length=80)
    year = models.PositiveIntegerField(_("year"))
    title = models.CharField(_("title"), max_length=200)
    citation = models.CharField(_("citation"), max_length=200, blank=True)
    opens_on = models.DateField(_("opens on"))
    due_date = models.DateField(_("due date"))
    status = models.CharField(
        _("status"), max_length=10, choices=PeriodStatus.choices, default=PeriodStatus.OPEN
    )
    path_id = models.CharField(_("chosen path"), max_length=80, blank=True)
    path_chosen_at = models.DateTimeField(null=True, blank=True)
    path_chosen_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # {exemption id: true/false} as answered by the board.
    exemption_answers = models.JSONField(_("exemption answers"), default=dict, blank=True)
    exemption_id = models.CharField(_("exemption"), max_length=80, blank=True)
    closed_on = models.DateField(_("closed on"), null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    missed = models.BooleanField(_("missed deadline"), default=False)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("compliance period")
        verbose_name_plural = _("compliance periods")
        ordering = ["due_date", "obligation_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "obligation_id", "year"],
                name="compliance_period_unique",
            )
        ]

    def __str__(self) -> str:
        return f"{self.title} {self.year}"

    @property
    def is_open(self) -> bool:
        return self.status == PeriodStatus.OPEN


class Evidence(AssociationScopedModel):
    """A document upload or a link (e.g. to an owner vote) proving the chosen path."""

    period = models.ForeignKey(ObligationPeriod, on_delete=models.PROTECT, related_name="evidence")
    file_key = models.CharField(max_length=500, blank=True)
    filename = models.CharField(_("file name"), max_length=255, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    url = models.CharField(_("link"), max_length=500, blank=True)
    note = models.CharField(_("note"), max_length=500, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("compliance evidence")
        verbose_name_plural = _("compliance evidence")
        ordering = ["added_at", "pk"]

    def __str__(self) -> str:
        return self.filename or self.url or self.note


class Recurrence(models.TextChoices):
    ONCE = "once", _("Once")
    MONTHLY = "monthly", _("Every month")
    QUARTERLY = "quarterly", _("Every quarter")
    YEARLY = "yearly", _("Every year")


RECURRENCE_MONTHS = {
    Recurrence.MONTHLY: 1,
    Recurrence.QUARTERLY: 3,
    Recurrence.YEARLY: 12,
}


def add_months(day: dt.date, months: int) -> dt.date:
    """``day`` moved by ``months``, clamped to the month's last day."""
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


class ManualItem(AssociationScopedModel):
    """A board-entered deadline the rule pack does not define (e.g. 1099s, insurance renewal).

    ``next_due`` is the next open occurrence; marking it done moves it on by the recurrence
    (a one-off item is closed instead). Occurrences step from ``anchor`` so a monthly item
    due on the 31st does not drift to the 28th."""

    title = models.CharField(_("title"), max_length=200)
    notes = models.CharField(_("notes"), max_length=500, blank=True)
    recurrence = models.CharField(
        _("repeats"), max_length=10, choices=Recurrence.choices, default=Recurrence.YEARLY
    )
    anchor = models.DateField(_("first due date"))
    next_due = models.DateField(_("next due date"))
    active = models.BooleanField(_("active"), default=True)
    last_done_on = models.DateField(_("last done on"), null=True, blank=True)
    last_done_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    last_done_document = models.ForeignKey(
        "documents.Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("proof of completion"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("compliance item")
        verbose_name_plural = _("compliance items")
        ordering = ["next_due", "title", "pk"]

    def __str__(self) -> str:
        return self.title

    def _offset(self) -> int:
        return (self.next_due.year - self.anchor.year) * 12 + (
            self.next_due.month - self.anchor.month
        )

    def due_dates(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """Open occurrences (from ``next_due`` on) in ``start``..``end``, inclusive."""
        if not self.active:
            return []
        if self.recurrence == Recurrence.ONCE:
            return [self.next_due] if start <= self.next_due <= end else []
        step = RECURRENCE_MONTHS[self.recurrence]
        months, out = self._offset(), []
        while (day := add_months(self.anchor, months)) <= end:
            if day >= start:
                out.append(day)
            months += step
        return out

    def first_due_after(self, day: dt.date | None) -> dt.date:
        """The first occurrence from ``anchor`` that falls after ``day`` (``anchor`` if None)."""
        if day is None or self.recurrence == Recurrence.ONCE or self.anchor > day:
            return self.anchor
        step = RECURRENCE_MONTHS[self.recurrence]
        months = 0
        while (due := add_months(self.anchor, months)) <= day:
            months += step
        return due

    def following(self) -> dt.date | None:
        """The occurrence after ``next_due``, or None for a one-off item."""
        if self.recurrence == Recurrence.ONCE:
            return None
        return add_months(self.anchor, self._offset() + RECURRENCE_MONTHS[self.recurrence])
