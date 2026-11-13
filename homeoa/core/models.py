from zoneinfo import available_timezones

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from .context import get_current_association


def validate_timezone(value: str) -> None:
    if value not in available_timezones():
        raise ValidationError(_("%(value)s is not a known time zone."), params={"value": value})


class Association(models.Model):
    """An HOA or condo association. Every other model belongs to one."""

    name = models.CharField(_("name"), max_length=200)
    short_name = models.CharField(
        _("short name"), max_length=12, help_text=_("Initials shown in the logo badge.")
    )
    timezone = models.CharField(
        _("time zone"), max_length=64, default="UTC", validators=[validate_timezone]
    )
    fiscal_year_start_month = models.PositiveSmallIntegerField(
        _("fiscal year start month"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    settings = models.JSONField(_("settings"), default=dict, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("association")
        verbose_name_plural = _("associations")
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(fiscal_year_start_month__gte=1, fiscal_year_start_month__lte=12),
                name="association_fiscal_month_range",
            )
        ]

    def __str__(self) -> str:
        return self.name


class AssociationScopedQuerySet(models.QuerySet):
    def for_association(self, association):
        return self.filter(association=association)


class AssociationScopedManager(models.Manager.from_queryset(AssociationScopedQuerySet)):
    """Filters to the current association. Fails closed: no association, no rows."""

    def get_queryset(self):
        qs = super().get_queryset()
        association = get_current_association()
        if association is None:
            return qs.none()
        return qs.filter(association=association)


class AssociationScopedModel(models.Model):
    """Abstract base for every association-owned model.

    ``objects`` is scoped to the current association (see ``homeoa.core.context``).
    ``all_associations`` is unscoped; use it only for cross-association admin code.
    New rows default to the current association when none is given.
    """

    association = models.ForeignKey(
        Association,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("association"),
    )

    objects = AssociationScopedManager()
    all_associations = AssociationScopedQuerySet.as_manager()

    class Meta:
        abstract = True
        base_manager_name = "all_associations"

    def save(self, *args, **kwargs):
        if self.association_id is None:
            current = get_current_association()
            if current is None:
                raise ValueError("No current association; set one or pass association=.")
            self.association = current
        super().save(*args, **kwargs)


class AssociationStatus(AssociationScopedModel):
    """Whether an association is still being set up or is live, and whether resident access
    is paused. Kept apart from ``Association.settings`` because that is saved whole.

    The status itself is not stored: the association is live once ``go_live_date`` has come.
    """

    association = models.OneToOneField(
        Association,
        on_delete=models.PROTECT,
        related_name="status",
        verbose_name=_("association"),
    )
    go_live_date = models.DateField(_("go-live date"), null=True, blank=True)
    residents_paused = models.BooleanField(_("resident access paused"), default=False)
    pause_reason = models.TextField(_("pause reason"), blank=True)
    paused_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("paused by"),
    )
    paused_at = models.DateTimeField(_("paused at"), null=True, blank=True)

    class Meta:
        verbose_name = _("association status")
        verbose_name_plural = _("association statuses")

    def __str__(self) -> str:
        return f"{self.association}: {self.go_live_date or '-'}"
