import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import MoneyField
from homeoa.core.models import AssociationScopedModel


class LedgerExport(AssociationScopedModel):
    """An itemized unit ledger exported for mediation (RCW 61.24.163(7)(a)), kept in the
    unit file. Immutable: each export stores its PDF and CSV as generated."""

    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    ownership_period = models.ForeignKey(
        "registry.OwnershipPeriod",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("ownership period"),
    )
    as_of = models.DateField(_("export date"))
    start = models.DateField(_("from"))
    past_due_since = models.DateField(_("past due since"), null=True, blank=True)
    balance = MoneyField(_("balance"))
    pdf_key = models.CharField(max_length=500)
    csv_key = models.CharField(max_length=500)
    pdf_sha256 = models.CharField(max_length=64)
    csv_sha256 = models.CharField(max_length=64)
    generated_at = models.DateTimeField(_("generated at"), auto_now_add=True)
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("ledger export")
        verbose_name_plural = _("ledger exports")
        ordering = ["-generated_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.unit} {self.start}–{self.as_of}"


class Category(models.TextChoices):
    GOVERNING = "governing", _("Governing")
    MINUTES = "minutes", _("Meetings")
    FINANCIAL = "financial", _("Financial")
    INSURANCE = "insurance", _("Insurance")
    CONTRACTS = "contracts", _("Contracts")
    FORMS = "forms", _("Forms")
    OTHER = "other", _("Other")


class Visibility(models.TextChoices):
    """Who may list and download a document; see ``documents.services.can_view``."""

    PUBLIC = "public", _("Public")
    TENANTS = "tenants", _("Owners & tenants")
    OWNERS = "owners", _("Owners")
    BOARD = "board", _("Board only")


class DocStatus(models.TextChoices):
    """Whether a filed document is still in force (e.g. a rescinded resolution)."""

    IN_FORCE = "", _("In force")
    RESCINDED = "rescinded", _("Rescinded")
    SUPERSEDED = "superseded", _("Superseded")


class GoverningKind(models.TextChoices):
    DECLARATION = "declaration", _("Declaration")
    BYLAWS = "bylaws", _("Bylaws")
    RULES = "rules", _("Rules")
    AMENDMENT = "amendment", _("Amendment")
    RESOLUTION = "resolution", _("Resolution")


class Document(AssociationScopedModel):
    """An association document (DOC-01). Its files are ``DocumentVersion`` rows; nothing
    is ever deleted. URLs use ``public_id`` so ids cannot be guessed."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField(_("title"), max_length=200)
    category = models.CharField(
        _("category"), max_length=12, choices=Category, default=Category.OTHER
    )
    visibility = models.CharField(
        _("visible to"), max_length=10, choices=Visibility, default=Visibility.BOARD
    )
    governing_kind = models.CharField(
        _("governing document kind"), max_length=12, choices=GoverningKind, blank=True
    )
    status = models.CharField(
        _("status"), max_length=12, choices=DocStatus, blank=True, default=DocStatus.IN_FORCE
    )
    status_note = models.CharField(_("status note"), max_length=300, blank=True)
    # Path to the record that changed the status (e.g. the rescinded resolution).
    status_link = models.CharField(_("status link"), max_length=300, blank=True)
    in_welcome_packet = models.BooleanField(
        _("in the owners' welcome packet"),
        default=False,
        help_text=_("Shown on the Welcome page new owners get. Not for board-only documents."),
    )
    current_version = models.ForeignKey(
        "DocumentVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("current version"),
    )
    updated_at = models.DateTimeField(_("updated"), auto_now=True)
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("document")
        verbose_name_plural = _("documents")
        ordering = ["-updated_at", "-pk"]

    def __str__(self) -> str:
        return self.title


class TextStatus(models.TextChoices):
    """How far the text inside a version's file was read for search (DOC-05)."""

    PENDING = "pending", _("Not read yet")
    READ = "read", _("Read")
    UNREAD = "unread", _("Scanned pages not read: reading scans (OCR) is off")
    EMPTY = "empty", _("No text found")
    FAILED = "failed", _("The file could not be read")


class DocumentVersion(AssociationScopedModel):
    """One uploaded file of a document. Governing documents carry an effective date and a
    note per version (GOV-01). Never deleted."""

    document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name="versions", verbose_name=_("document")
    )
    number = models.PositiveIntegerField(_("version"))
    file_key = models.CharField(max_length=500)
    file_name = models.CharField(_("file name"), max_length=200)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField(_("size"))
    sha256 = models.CharField(max_length=64)
    effective_date = models.DateField(_("effective date"), null=True, blank=True)
    note = models.CharField(_("note"), max_length=500, blank=True)
    uploaded_at = models.DateTimeField(_("uploaded"), auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    text_status = models.CharField(
        _("text for search"), max_length=8, choices=TextStatus, default=TextStatus.PENDING
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("document version")
        verbose_name_plural = _("document versions")
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["document", "number"], name="docversion_number")
        ]

    def __str__(self) -> str:
        return f"{self.document} v{self.number}"


class DocumentPage(AssociationScopedModel):
    """The text of one page of a document version, for search (DOC-05). A file
    without pages (text, CSV, Word, Excel, a photo) is one page. Derived from the file and
    rebuilt whole whenever the version is read again."""

    version = models.ForeignKey(
        DocumentVersion, on_delete=models.CASCADE, related_name="pages", verbose_name=_("version")
    )
    number = models.PositiveIntegerField(_("page"))
    text = models.TextField(_("text"), blank=True)
    ocr = models.BooleanField(_("read from a scan"), default=False)
    search = SearchVectorField(null=True, editable=False)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("document page")
        verbose_name_plural = _("document pages")
        ordering = ["version", "number"]
        constraints = [models.UniqueConstraint(fields=["version", "number"], name="docpage_number")]
        indexes = [GinIndex(fields=["search"], name="docpage_search")]

    def __str__(self) -> str:
        return f"{self.version} p{self.number}"


class WelcomePacket(AssociationScopedModel):
    """The owners' welcome packet (RES-03): the board's message. Its documents are the
    ones marked ``in_welcome_packet``."""

    message = models.TextField(_("welcome message"), blank=True)
    updated_at = models.DateTimeField(_("updated"), auto_now=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("welcome packet")
        verbose_name_plural = _("welcome packets")
        constraints = [
            models.UniqueConstraint(fields=["association"], name="documents_welcome_one")
        ]

    def __str__(self) -> str:
        return str(self._meta.verbose_name)


class WelcomeSeen(AssociationScopedModel):
    """When an owner first opened the Welcome page; until then Home shows a card for it."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    seen_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["association", "user"], name="documents_welcome_seen_once"
            )
        ]
