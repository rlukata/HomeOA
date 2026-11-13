"""Import runs, saved mapping profiles, external-id keys and account mappings (REG-05)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class Source(models.TextChoices):
    ECONDO = "econdo", _("AppFolio-style export")
    CSV = "csv", _("Other CSV files")


class MappingProfile(AssociationScopedModel):
    """A saved, reusable mapping: which file is which, column choices, transforms."""

    name = models.CharField(_("name"), max_length=100)
    source = models.CharField(_("source"), max_length=20, choices=Source.choices)
    config = models.JSONField(_("settings"), default=dict)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("import mapping")
        verbose_name_plural = _("import mappings")
        ordering = ["-updated_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "source", "name"], name="imports_profile_unique_name"
            )
        ]

    def __str__(self) -> str:
        return self.name


class ImportRun(AssociationScopedModel):
    class Mode(models.TextChoices):
        COMMIT = "commit", _("Load into HomeOA's books")
        COMPARISON = "comparison", _("Keep for comparison only (parallel run)")

    class Status(models.TextChoices):
        UPLOADED = "uploaded", _("Uploaded")
        PREVIEWED = "previewed", _("Previewed")
        COMMITTED = "committed", _("Imported")
        STORED = "stored", _("Saved for comparison")
        FAILED = "failed", _("Failed")

    source = models.CharField(_("source"), max_length=20, choices=Source.choices)
    mode = models.CharField(_("mode"), max_length=20, choices=Mode.choices, default=Mode.COMMIT)
    as_of = models.DateField(_("as of"))
    profile = models.ForeignKey(
        MappingProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="runs",
        verbose_name=_("mapping"),
    )
    config = models.JSONField(_("mapping used"), default=dict)
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.UPLOADED
    )
    report = models.JSONField(_("report"), default=dict, blank=True)
    # Comparison mode: the parsed snapshot (JSON-safe strings) for the parallel-run diff.
    snapshot = models.JSONField(_("snapshot"), default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("uploaded by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)
    # Raw file contents are purged after the retention period (REG-05 privacy).
    purged_at = models.DateTimeField(_("files purged at"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("import run")
        verbose_name_plural = _("import runs")
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return str(_("Import #%(n)s") % {"n": self.pk})

    @property
    def is_done(self) -> bool:
        return self.status in {self.Status.COMMITTED, self.Status.STORED}


class ImportFile(AssociationScopedModel):
    run = models.ForeignKey(
        ImportRun, on_delete=models.CASCADE, related_name="files", verbose_name=_("import")
    )
    name = models.CharField(_("file name"), max_length=255)
    content = models.TextField(_("content"), blank=True)
    sha256 = models.CharField(_("SHA-256"), max_length=64, blank=True)
    size = models.PositiveIntegerField(_("size (characters)"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("import file")
        verbose_name_plural = _("import files")
        ordering = ["name", "pk"]

    def __str__(self) -> str:
        return self.name


class ExternalRef(AssociationScopedModel):
    """Maps a source record (by its external key) to the HomeOA object it created.

    Re-imports look records up here, so they update instead of duplicating. ``fingerprint``
    is the last imported value set; ledger rows compare it to decide whether to repost.
    """

    source = models.CharField(_("source"), max_length=20)
    kind = models.CharField(_("kind"), max_length=30)
    external_id = models.CharField(_("external id"), max_length=255)
    object_id = models.BigIntegerField(_("object id"))
    fingerprint = models.CharField(_("fingerprint"), max_length=64, blank=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("external reference")
        verbose_name_plural = _("external references")
        constraints = [
            models.UniqueConstraint(
                fields=["association", "source", "kind", "external_id"],
                name="imports_extref_unique",
            )
        ]

    def __str__(self) -> str:
        return f"{self.source}:{self.kind}:{self.external_id}"


class AccountMapping(AssociationScopedModel):
    """Saved mapping of a source (eCondo) account to a HomeOA account and fund."""

    source = models.CharField(_("source"), max_length=20, choices=Source.choices)
    external_code = models.CharField(_("source account number"), max_length=40)
    external_name = models.CharField(_("source account name"), max_length=200, blank=True)
    account = models.ForeignKey(
        "ledger.Account", on_delete=models.PROTECT, related_name="+", verbose_name=_("account")
    )
    fund = models.ForeignKey(
        "ledger.Fund", on_delete=models.PROTECT, related_name="+", verbose_name=_("fund")
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("account mapping")
        verbose_name_plural = _("account mappings")
        ordering = ["external_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "source", "external_code"],
                name="imports_account_mapping_unique",
            )
        ]

    def __str__(self) -> str:
        return f"{self.external_code} → {self.account}"


from .diff.models import DiffLine, DiffRun  # noqa: E402, F401  (registers the diff models)
