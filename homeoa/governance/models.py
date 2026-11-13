"""Board roster, committees, resolutions and action items (GOV-02, GOV-06).

Terms and committee seats record who served; they never change anyone's login role
(roles are assigned in Settings > Roles).
"""

from __future__ import annotations

import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


def _check_dates(start, end):
    if start and end and end < start:
        raise ValidationError(_("The end date cannot be before the start date."))


def _covers(start, end, on: datetime.date) -> bool:
    return start <= on and (end is None or end >= on)


class Office(models.TextChoices):
    PRESIDENT = "president", _("President")
    VICE_PRESIDENT = "vice_president", _("Vice president")
    SECRETARY = "secretary", _("Secretary")
    TREASURER = "treasurer", _("Treasurer")
    DIRECTOR = "director", _("Director at large")


class BoardTerm(AssociationScopedModel):
    """One person's term on the board in one office."""

    person = models.ForeignKey(
        "registry.Person",
        on_delete=models.PROTECT,
        related_name="board_terms",
        verbose_name=_("person"),
    )
    office = models.CharField(_("office"), max_length=20, choices=Office.choices)
    start = models.DateField(_("start"))
    end = models.DateField(_("end"), null=True, blank=True)
    note = models.CharField(_("note"), max_length=200, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("board term")
        verbose_name_plural = _("board terms")
        ordering = ["start", "pk"]

    def __str__(self) -> str:
        return f"{self.person} - {self.get_office_display()}"

    def clean(self):
        _check_dates(self.start, self.end)

    def covers(self, on: datetime.date) -> bool:
        return _covers(self.start, self.end, on)


class Committee(AssociationScopedModel):
    name = models.CharField(_("name"), max_length=120)
    purpose = models.CharField(_("purpose"), max_length=300, blank=True)
    active = models.BooleanField(_("active"), default=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("committee")
        verbose_name_plural = _("committees")
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "name"], name="governance_committee_name_unique"
            )
        ]

    def __str__(self) -> str:
        return self.name


class CommitteeMember(AssociationScopedModel):
    committee = models.ForeignKey(
        Committee, on_delete=models.CASCADE, related_name="members", verbose_name=_("committee")
    )
    person = models.ForeignKey(
        "registry.Person",
        on_delete=models.PROTECT,
        related_name="committee_seats",
        verbose_name=_("person"),
    )
    is_chair = models.BooleanField(_("chair"), default=False)
    start = models.DateField(_("start"))
    end = models.DateField(_("end"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("committee member")
        verbose_name_plural = _("committee members")
        ordering = ["-is_chair", "person__full_name", "pk"]

    def __str__(self) -> str:
        return f"{self.person} - {self.committee}"

    def clean(self):
        _check_dates(self.start, self.end)

    def covers(self, on: datetime.date) -> bool:
        return _covers(self.start, self.end, on)


class Visibility(models.TextChoices):
    """Who may read a filed document (matches the document library's audiences)."""

    PUBLIC = "public", _("Public")
    OWNERS = "owners", _("Owners")
    TENANTS = "tenants", _("Owners and tenants")
    BOARD = "board", _("Board only")


class ImmutableResolutionError(Exception):
    pass


class Resolution(AssociationScopedModel):
    """A board resolution, numbered per year (``2026-03``).

    Once adopted it is immutable; the only allowed change is rescinding it (audited).
    """

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        ADOPTED = "adopted", _("Adopted")
        RESCINDED = "rescinded", _("Rescinded")

    year = models.PositiveIntegerField(_("year"))
    seq = models.PositiveIntegerField(_("sequence"))
    title = models.CharField(_("title"), max_length=200)
    text = models.TextField(_("text"), blank=True)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    adopted_on = models.DateField(_("adopted on"), null=True, blank=True)
    rescinded_on = models.DateField(_("rescinded on"), null=True, blank=True)
    rescind_reason = models.CharField(_("reason for rescinding"), max_length=300, blank=True)
    # The signed or printed PDF, filed in the document library when adopted (``document``);
    # ``pdf_key`` is that version's storage key.
    document = models.ForeignKey(
        "documents.Document",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("document"),
    )
    pdf_key = models.CharField(max_length=500, blank=True)
    pdf_filename = models.CharField(_("PDF file name"), max_length=255, blank=True)
    pdf_visibility = models.CharField(
        _("PDF visible to"), max_length=10, choices=Visibility.choices, default=Visibility.OWNERS
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # The meeting that adopted it, when known.
    meeting = models.ForeignKey(
        "meetings.Meeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolutions",
        verbose_name=_("meeting"),
    )

    # Fields that may still change after adoption (rescinding).
    RESCIND_FIELDS = frozenset({"status", "rescinded_on", "rescind_reason"})

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("resolution")
        verbose_name_plural = _("resolutions")
        ordering = ["-year", "-seq"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "year", "seq"], name="governance_resolution_number_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.number} {self.title}"

    @property
    def number(self) -> str:
        return f"{self.year}-{self.seq:02d}"

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).all_associations.filter(pk=self.pk).first()
            if old is not None and old.status != self.Status.DRAFT:
                changed = {
                    f.attname
                    for f in self._meta.concrete_fields
                    if getattr(old, f.attname) != getattr(self, f.attname)
                }
                if old.status == self.Status.RESCINDED and changed:
                    raise ImmutableResolutionError("rescinded resolutions are final")
                if changed - self.RESCIND_FIELDS or self.status == self.Status.DRAFT:
                    raise ImmutableResolutionError("adopted resolutions can only be rescinded")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise ImmutableResolutionError("adopted resolutions cannot be deleted")
        return super().delete(*args, **kwargs)


class Vote(models.TextChoices):
    FOR = "for", _("For")
    AGAINST = "against", _("Against")
    ABSTAIN = "abstain", _("Abstain")


class ResolutionVote(AssociationScopedModel):
    """How one board member voted on a resolution (locked once adopted)."""

    resolution = models.ForeignKey(
        Resolution, on_delete=models.CASCADE, related_name="votes", verbose_name=_("resolution")
    )
    person = models.ForeignKey(
        "registry.Person", on_delete=models.PROTECT, related_name="+", verbose_name=_("person")
    )
    vote = models.CharField(_("vote"), max_length=10, choices=Vote.choices)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("resolution vote")
        verbose_name_plural = _("resolution votes")
        ordering = ["person__full_name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["resolution", "person"], name="governance_vote_once_per_person"
            )
        ]

    def __str__(self) -> str:
        return f"{self.person}: {self.get_vote_display()}"

    def save(self, *args, **kwargs):
        if not Resolution.all_associations.filter(
            pk=self.resolution_id, status=Resolution.Status.DRAFT
        ).exists():
            raise ImmutableResolutionError("votes are locked once a resolution is adopted")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not self.resolution.is_draft:
            raise ImmutableResolutionError("votes are locked once a resolution is adopted")
        return super().delete(*args, **kwargs)


class ActionItem(AssociationScopedModel):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        DONE = "done", _("Done")
        CANCELLED = "cancelled", _("Cancelled")

    title = models.CharField(_("title"), max_length=200)
    owner = models.ForeignKey(
        "registry.Person",
        on_delete=models.PROTECT,
        related_name="action_items",
        verbose_name=_("owner"),
    )
    due_date = models.DateField(_("due date"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default="open")
    resolution = models.ForeignKey(
        Resolution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="action_items",
        verbose_name=_("resolution"),
    )
    notes = models.TextField(_("notes"), blank=True)
    completed_on = models.DateField(_("completed on"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("action item")
        verbose_name_plural = _("action items")
        ordering = [models.F("due_date").asc(nulls_last=True), "pk"]
        indexes = [models.Index(fields=["association", "status"], name="gov_action_status_idx")]

    def __str__(self) -> str:
        return self.title


class ContactMessage(AssociationScopedModel):
    """A message from a resident or member to the board, a committee or the system admin.
    Residents see names and titles, never members' emails; the message is emailed
    to the members and kept here for the board's inbox (Community > Messages)."""

    class To(models.TextChoices):
        BOARD = "board", _("The board")
        COMMITTEE = "committee", _("A committee")
        SYS_ADMIN = "sys_admin", _("The system admin")

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("from"),
    )
    to = models.CharField(_("to"), max_length=10, choices=To.choices)
    committee = models.ForeignKey(
        Committee,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="messages",
        verbose_name=_("committee"),
    )
    subject = models.CharField(_("subject"), max_length=150)
    body = models.TextField(_("message"))
    sent_at = models.DateTimeField(_("sent at"), auto_now_add=True)
    recipient_count = models.PositiveSmallIntegerField(_("sent to"), default=0)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("contact message")
        verbose_name_plural = _("contact messages")
        ordering = ["-sent_at", "-pk"]

    def __str__(self) -> str:
        return self.subject

    @property
    def to_label(self) -> str:
        if self.to == self.To.COMMITTEE and self.committee_id:
            return str(self.committee)
        return str(self.get_to_display())
