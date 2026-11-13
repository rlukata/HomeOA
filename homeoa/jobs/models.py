from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class ScheduledRun(AssociationScopedModel):
    """One periodic job run for one association and one as-of date."""

    class Status(models.TextChoices):
        RUNNING = "running", _("Running")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Failed")

    job_name = models.CharField(_("job"), max_length=200)
    as_of = models.DateField(_("as of"))
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.RUNNING
    )
    attempts = models.PositiveIntegerField(_("attempts"), default=0)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)
    error = models.TextField(_("error"), blank=True)

    class Meta:
        verbose_name = _("scheduled run")
        verbose_name_plural = _("scheduled runs")
        ordering = ["-as_of", "job_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "job_name", "as_of"], name="jobs_scheduledrun_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.job_name} {self.as_of} ({self.status})"
