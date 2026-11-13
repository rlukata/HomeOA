"""Letter templates (versioned) and generated letters (COM-03)."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class ImmutableLetterError(Exception):
    pass


class LetterTemplate(AssociationScopedModel):
    """A board-editable letter. Its wording lives in immutable versions."""

    class Kind(models.TextChoices):
        STATEMENT_COVER = "statement_cover", _("Statement cover")
        REMINDER = "reminder", _("Payment reminder")
        LATE_NOTICE = "late_notice", _("Late notice")
        DEMAND = "demand", _("Demand letter")
        DELINQUENCY_NOTICE = "delinquency_notice", _("Statutory notice of delinquency")
        GENERAL = "general", _("General letter")

    key = models.SlugField(_("key"), max_length=60)
    name = models.CharField(_("name"), max_length=120)
    kind = models.CharField(_("kind"), max_length=30, choices=Kind, default=Kind.GENERAL)
    draft_wording = models.BooleanField(
        _("draft wording, not legal advice"),
        default=False,
        help_text=_("Clear this once the wording has been reviewed by the association's lawyer."),
    )
    current_version = models.ForeignKey(
        "LetterTemplateVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("current version"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("letter template")
        verbose_name_plural = _("letter templates")
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["association", "key"], name="letters_template_key")
        ]

    def __str__(self) -> str:
        return self.name


class LetterTemplateVersion(AssociationScopedModel):
    """One saved wording of a template. Never changed after it is saved."""

    template = models.ForeignKey(LetterTemplate, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField(_("version"))
    subject = models.CharField(_("subject"), max_length=200, blank=True)
    body = models.TextField(_("body"))
    note = models.CharField(_("change note"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = _("letter template version")
        verbose_name_plural = _("letter template versions")
        ordering = ["template", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["template", "number"], name="letters_version_number")
        ]

    def __str__(self) -> str:
        return f"{self.template} v{self.number}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ImmutableLetterError("Template versions cannot be changed; save a new one.")
        super().save(*args, **kwargs)


class Letter(AssociationScopedModel):
    """A generated letter PDF, linked to the unit and the exact template version used."""

    template_version = models.ForeignKey(
        LetterTemplateVersion,
        on_delete=models.PROTECT,
        related_name="letters",
        verbose_name=_("template version"),
    )
    unit = models.ForeignKey(
        "registry.Unit",
        on_delete=models.PROTECT,
        related_name="letters",
        verbose_name=_("unit"),
    )
    recipient_name = models.CharField(_("recipient"), max_length=300, blank=True)
    address_lines = models.JSONField(_("mailing address"), default=list, blank=True)
    subject = models.CharField(_("subject"), max_length=200, blank=True)
    as_of = models.DateField(_("letter date"))
    file_key = models.CharField(_("file"), max_length=500)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveIntegerField(default=0)
    generated_at = models.DateTimeField(_("generated at"), auto_now_add=True)
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = _("letter")
        verbose_name_plural = _("letters")
        ordering = ["-generated_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.template_version.template} · {self.unit} · {self.as_of}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ImmutableLetterError("Generated letters cannot be changed.")
        super().save(*args, **kwargs)


class MailBatch(AssociationScopedModel):
    """Generated letters going out by postal mail together (COM-04).

    A batch is a draft until it is sent. With the default provider sending queues it for
    printing ("ready"), and the board confirms once it is mailed by hand ("mailed"). With
    a mail-service plugin (Lob) sending submits every letter ("submitted"). While
    residents are off a batch stays a draft.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        READY = "ready", _("Ready to print")
        MAILED = "mailed", _("Printed (mailed by hand)")
        SUBMITTED = "submitted", _("Sent to mail service")

    name = models.CharField(_("name"), max_length=120)
    status = models.CharField(_("status"), max_length=20, choices=Status, default=Status.DRAFT)
    certified = models.BooleanField(_("certified mail"), default=False)
    provider = models.CharField(_("sent through"), max_length=60, blank=True)
    pdf_key = models.CharField(_("print file"), max_length=500, blank=True)
    manifest_key = models.CharField(_("manifest"), max_length=500, blank=True)
    pages = models.PositiveIntegerField(_("pages"), default=0)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    confirmed_at = models.DateTimeField(_("mailed at"), null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = _("mail batch")
        verbose_name_plural = _("mail batches")
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return self.name


class MailItem(AssociationScopedModel):
    """One letter in a batch, with its mail status and the notify deliveries it fulfils."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Not sent")
        QUEUED_FOR_PRINT = "queued_for_print", _("Ready to print")
        MAILED_BY_HAND = "mailed_by_hand", _("Printed (mailed by hand)")
        SUBMITTED = "submitted", _("Sent to mail service")
        IN_TRANSIT = "in_transit", _("In transit")
        DELIVERED = "delivered", _("Delivered")
        RETURNED = "returned", _("Returned to sender")
        FAILED = "failed", _("Failed")

    batch = models.ForeignKey(MailBatch, on_delete=models.CASCADE, related_name="items")
    letter = models.ForeignKey(
        Letter, on_delete=models.PROTECT, related_name="mail_items", verbose_name=_("letter")
    )
    deliveries = models.ManyToManyField("notify.Delivery", blank=True, related_name="mail_items")
    status = models.CharField(_("status"), max_length=20, choices=Status, default=Status.PENDING)
    provider_id = models.CharField(_("provider id"), max_length=200, blank=True, db_index=True)
    tracking = models.CharField(_("tracking number"), max_length=200, blank=True)
    error = models.TextField(_("error"), blank=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("mail item")
        verbose_name_plural = _("mail items")
        ordering = ["batch", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "letter"], name="letters_mailitem_unique")
        ]

    def __str__(self) -> str:
        return f"{self.batch} · {self.letter}"


class LetterTranslation(AssociationScopedModel):
    """A board-approved translation of a template into an owner's preferred language.

    HomeOA never machine-translates: the board uploads the translation (PDF or text) and,
    by uploading it, approves it. The newest upload per (template, language) is used;
    older ones are kept for the record and never changed.
    """

    template = models.ForeignKey(
        LetterTemplate,
        on_delete=models.CASCADE,
        related_name="translations",
        verbose_name=_("template"),
    )
    language = models.CharField(_("language"), max_length=10)
    text = models.TextField(_("translated text"), blank=True)
    filename = models.CharField(_("file name"), max_length=200, blank=True)
    file_key = models.CharField(_("file"), max_length=500, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    size = models.PositiveIntegerField(default=0)
    note = models.CharField(_("note"), max_length=200, blank=True)
    uploaded_at = models.DateTimeField(_("uploaded at"), auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = _("letter translation")
        verbose_name_plural = _("letter translations")
        ordering = ["template", "language", "-uploaded_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.template} · {self.language}"

    @property
    def is_pdf(self) -> bool:
        return bool(self.file_key)

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ImmutableLetterError("Translations cannot be changed; upload a new one.")
        super().save(*args, **kwargs)
