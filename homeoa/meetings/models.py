"""Meetings, agendas and the notice sent for them (GOV-03)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class Meeting(AssociationScopedModel):
    class Kind(models.TextChoices):
        BOARD = "board", _("Board meeting")
        ANNUAL = "owners_annual", _("Annual owners meeting")
        SPECIAL = "owners_special", _("Special owners meeting")

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        NOTICED = "noticed", _("Noticed")
        HELD = "held", _("Held")
        CANCELLED = "cancelled", _("Cancelled")

    kind = models.CharField(_("kind"), max_length=20, choices=Kind.choices)
    title = models.CharField(_("title"), max_length=200, blank=True)
    date = models.DateField(_("date"))
    start_time = models.TimeField(_("start time"), null=True, blank=True)
    location = models.CharField(_("location"), max_length=300, blank=True)
    video_url = models.URLField(_("video link"), max_length=500, blank=True)
    dial_in = models.CharField(
        _("join by phone"),
        max_length=200,
        blank=True,
        help_text=_("Phone number and access code for joining by telephone."),
    )
    description = models.TextField(_("description"), blank=True)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    noticed_on = models.DateField(_("notice sent on"), null=True, blank=True)
    # A board meeting called for an unforeseeable event (short window, email only).
    emergency = models.BooleanField(_("emergency meeting"), default=False)
    emergency_reason = models.TextField(_("emergency reason"), blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("meeting")
        verbose_name_plural = _("meetings")
        ordering = ["-date", "-start_time", "-pk"]
        indexes = [models.Index(fields=["association", "status"], name="meeting_status_idx")]

    def __str__(self) -> str:
        return self.display_title

    @property
    def display_title(self) -> str:
        return self.title or str(self.get_kind_display())

    @property
    def is_owner_meeting(self) -> bool:
        return self.kind != self.Kind.BOARD

    @property
    def is_open(self) -> bool:
        return self.status in (self.Status.DRAFT, self.Status.NOTICED)


class AgendaItem(AssociationScopedModel):
    meeting = models.ForeignKey(
        Meeting, on_delete=models.CASCADE, related_name="agenda", verbose_name=_("meeting")
    )
    position = models.PositiveIntegerField(_("position"), default=0)
    title = models.CharField(_("title"), max_length=200)
    notes = models.TextField(_("notes"), blank=True)
    resolution = models.ForeignKey(
        "governance.Resolution",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agenda_items",
        verbose_name=_("resolution"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("agenda item")
        verbose_name_plural = _("agenda items")
        ordering = ["position", "pk"]

    def __str__(self) -> str:
        return self.title


class MeetingNotice(AssociationScopedModel):
    """One notice sent for a meeting: to a unit's owners (email or letter), as a hand-mailed
    print-only letter, or to one board member before cutover. Kept as the copy."""

    class Channel(models.TextChoices):
        EMAIL = "email", _("Email")
        MAIL = "mail", _("Letter by mail")
        PRINT = "print", _("Print only (mailed by hand)")
        BOARD = "board", _("Board copy")

    meeting = models.ForeignKey(
        Meeting, on_delete=models.CASCADE, related_name="notices", verbose_name=_("meeting")
    )
    unit = models.ForeignKey(
        "registry.Unit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("unit"),
    )
    person = models.ForeignKey(
        "registry.Person",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("person"),
    )
    channel = models.CharField(_("channel"), max_length=10, choices=Channel.choices)
    subject = models.CharField(_("subject"), max_length=300)
    body = models.TextField(_("body"))
    letter = models.ForeignKey(
        "letters.Letter", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    batch = models.ForeignKey(
        "letters.MailBatch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    sent_on = models.DateField(_("sent on"))
    created_at = models.DateTimeField(auto_now_add=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("meeting notice")
        verbose_name_plural = _("meeting notices")
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["meeting", "unit"],
                condition=models.Q(unit__isnull=False),
                name="meeting_notice_once_per_unit",
            ),
            models.UniqueConstraint(
                fields=["meeting", "person"],
                condition=models.Q(person__isnull=False),
                name="meeting_notice_once_per_person",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.meeting} - {self.unit or self.person}"


class Minutes(AssociationScopedModel):
    """Minutes of one meeting (GOV-04): draft (editable), approved at a later meeting
    (immutable), then published as a PDF in the document library."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        APPROVED = "approved", _("Approved")
        PUBLISHED = "published", _("Published")

    meeting = models.OneToOneField(
        Meeting, on_delete=models.PROTECT, related_name="minutes", verbose_name=_("meeting")
    )
    body = models.TextField(_("minutes"), blank=True)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    approved_at = models.ForeignKey(
        Meeting,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="minutes_approved",
        verbose_name=_("approved at meeting"),
    )
    approved_on = models.DateField(_("approved on"), null=True, blank=True)
    document = models.ForeignKey(
        "documents.Document",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("published document"),
    )
    published_on = models.DateField(_("published on"), null=True, blank=True)
    envelope = models.ForeignKey(
        "esign.Envelope", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)
    drafted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("minutes")
        verbose_name_plural = _("minutes")

    def __str__(self) -> str:
        return f"{self.meeting} - {self.get_status_display()}"

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    def save(self, *args, **kwargs):
        # Approved text is immutable: refuse any change to the body once approved.
        if self.pk:
            old = type(self).all_associations.filter(pk=self.pk).values("body", "status").first()
            if (
                old
                and old["status"] != self.Status.DRAFT
                and (old["body"] != self.body or self.status == self.Status.DRAFT)
            ):
                raise ValueError("approved minutes cannot be changed")
        super().save(*args, **kwargs)


class Attendance(AssociationScopedModel):
    """Who attended: a unit (owners meeting) or a board member (board meeting). Proxies
    are kept as ``Proxy`` records and show as "by proxy"."""

    class Mode(models.TextChoices):
        IN_PERSON = "in_person", _("In person")
        REMOTE = "remote", _("Remote")

    meeting = models.ForeignKey(
        Meeting, on_delete=models.CASCADE, related_name="attendance", verbose_name=_("meeting")
    )
    unit = models.ForeignKey(
        "registry.Unit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("unit"),
    )
    person = models.ForeignKey(
        "registry.Person",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("person"),
    )
    mode = models.CharField(_("attended"), max_length=10, choices=Mode.choices)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("attendance")
        verbose_name_plural = _("attendance")
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["meeting", "unit"],
                condition=models.Q(unit__isnull=False),
                name="meeting_attendance_once_per_unit",
            ),
            models.UniqueConstraint(
                fields=["meeting", "person"],
                condition=models.Q(person__isnull=False),
                name="meeting_attendance_once_per_person",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.meeting} - {self.unit or self.person}"


class Proxy(AssociationScopedModel):
    """A unit's proxy for one owners meeting (GOV-05). Revocable before the meeting."""

    meeting = models.ForeignKey(
        Meeting, on_delete=models.CASCADE, related_name="proxies", verbose_name=_("meeting")
    )
    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("grantor unit")
    )
    holder = models.CharField(_("proxy holder"), max_length=200)
    granted_on = models.DateField(_("dated"))
    document = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("signed proxy"),
    )
    revoked_on = models.DateField(_("revoked on"), null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("proxy")
        verbose_name_plural = _("proxies")
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["meeting", "unit"],
                condition=models.Q(revoked_on__isnull=True),
                name="meeting_proxy_one_active_per_unit",
            )
        ]

    def __str__(self) -> str:
        return f"{self.unit} -> {self.holder}"

    @property
    def active(self) -> bool:
        return self.revoked_on is None


class AnnualWorkflow(AssociationScopedModel):
    """The annual meeting checklist (GOV-03, FIN-13): budget summary to owners, the
    ratification vote and the audit-waiver vote. Dates come from the rule pack; see
    :mod:`homeoa.meetings.annual`."""

    class Outcome(models.TextChoices):
        PENDING = "", _("Pending")
        RATIFIED = "ratified", _("Ratified")
        REJECTED = "rejected", _("Rejected")
        WAIVED = "waived", _("Audit waived")
        NOT_WAIVED = "not_waived", _("Audit not waived")

    meeting = models.OneToOneField(
        Meeting, on_delete=models.CASCADE, related_name="annual", verbose_name=_("meeting")
    )
    budget = models.ForeignKey(
        "budget.Budget",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("budget"),
    )
    budget_sent_on = models.DateField(_("budget summary sent on"), null=True, blank=True)
    ratification_vote = models.OneToOneField(
        "votes.Vote",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("ratification vote"),
    )
    waiver_vote = models.OneToOneField(
        "votes.Vote",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("audit-waiver vote"),
    )
    ratification_outcome = models.CharField(
        _("ratification result"), max_length=12, choices=Outcome.choices, blank=True
    )
    waiver_outcome = models.CharField(
        _("audit-waiver result"), max_length=12, choices=Outcome.choices, blank=True
    )
    # The board dismissed the "budget rejected" to-do.
    rejection_dismissed_at = models.DateTimeField(
        _("rejection follow-up dismissed at"), null=True, blank=True
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("annual meeting workflow")
        verbose_name_plural = _("annual meeting workflows")

    def __str__(self) -> str:
        return str(self.meeting)
