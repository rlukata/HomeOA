"""Architectural review (ARC-01, ARC-02): configurable forms, requests, comments, votes.

The decision is always a person's act (``services.decide``); votes are advisory and only
counted for display.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class FieldKind(models.TextChoices):
    TEXT = "text", _("Short text")
    TEXTAREA = "textarea", _("Long text")
    DATE = "date", _("Date")
    CHOICE = "choice", _("Choice")
    CHECKBOX = "checkbox", _("Checkbox")


class StatutoryKind(models.TextChoices):
    """Requests the law deems approved when not denied in time (rule keys
    ``arc.<kind>.deemed_approval_days``; WA: RCW 64.90.513 and .580)."""

    NONE = "", _("None")
    EV_CHARGER = "ev_charger", _("EV charging station")
    HEAT_PUMP = "heat_pump", _("Heat pump")


class ArcForm(AssociationScopedModel):
    """A request form. ``fields`` is a list of
    ``{"key", "label", "kind", "required", "choices": [..], "help"}`` dicts."""

    name = models.CharField(_("name"), max_length=120)
    description = models.TextField(_("description"), blank=True)
    fields = models.JSONField(_("fields"), default=list, blank=True)
    active = models.BooleanField(_("active"), default=True)
    statutory_kind = models.CharField(
        _("deadline set by law"),
        max_length=20,
        choices=StatutoryKind.choices,
        blank=True,
        default="",
        help_text=_(
            "For EV charger or heat pump forms, the law may deem a request approved when "
            "it is not denied in writing in time."
        ),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("architectural request form")
        verbose_name_plural = _("architectural request forms")
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["association", "name"], name="arc_form_name_unique")
        ]

    def __str__(self) -> str:
        return self.name


class ArcRequest(AssociationScopedModel):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", _("Submitted")
        INFO_REQUESTED = "info_requested", _("Information requested")
        UNDER_REVIEW = "under_review", _("Under review")
        APPROVED = "approved", _("Approved")
        APPROVED_WITH_CONDITIONS = "approved_with_conditions", _("Approved with conditions")
        DENIED = "denied", _("Denied")
        WITHDRAWN = "withdrawn", _("Withdrawn")

    OPEN = frozenset({"submitted", "info_requested", "under_review"})
    # The decision clock runs only while the board has the request (not while waiting on
    # the owner's answer).
    WAITING_ON_BOARD = ("submitted", "under_review")
    DECISIONS = ("approved", "approved_with_conditions", "denied")

    unit = models.ForeignKey(
        "registry.Unit",
        on_delete=models.PROTECT,
        related_name="arc_requests",
        verbose_name=_("unit"),
    )
    submitted_by = models.ForeignKey(
        "registry.Person",
        on_delete=models.PROTECT,
        related_name="arc_requests",
        verbose_name=_("requested by"),
    )
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("entered by"),
    )
    form = models.ForeignKey(
        ArcForm, on_delete=models.PROTECT, related_name="requests", verbose_name=_("form")
    )
    # The form's field definitions as submitted, so later form edits never change history.
    form_fields = models.JSONField(default=list, blank=True)
    title = models.CharField(_("title"), max_length=200)
    answers = models.JSONField(_("answers"), default=dict, blank=True)
    status = models.CharField(
        _("status"), max_length=30, choices=Status.choices, default=Status.SUBMITTED
    )
    conditions = models.TextField(_("conditions"), blank=True)
    decision_note = models.TextField(_("decision note"), blank=True)
    submitted_on = models.DateField(_("submitted on"))
    # The decision clock: starts at submission and restarts when the owner answers
    # a request for more information. ``decide_by`` = clock start + ``arc.decision_days``.
    clock_started_on = models.DateField(_("review clock started on"), null=True, blank=True)
    decide_by = models.DateField(_("decide by"), null=True, blank=True)
    # Deemed approval set by law (``arc.deemed``): the last day to deny in writing is
    # ``submitted_on`` + the rule's days + the days spent waiting on the owner's answers.
    statutory_kind = models.CharField(
        _("deadline set by law"),
        max_length=20,
        choices=StatutoryKind.choices,
        blank=True,
        default="",
    )
    info_requested_on = models.DateField(null=True, blank=True)
    paused_days = models.PositiveIntegerField(default=0)
    deny_by = models.DateField(_("deny in writing by"), null=True, blank=True)
    deemed = models.BooleanField(_("deemed approved by law"), default=False)
    citations = models.TextField(
        _("citations"),
        blank=True,
        help_text=_("Sections of the declaration or rules the decision relies on."),
    )
    decision_letter = models.ForeignKey(
        "letters.Letter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("decision letter"),
    )
    decided_on = models.DateField(_("decided on"), null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("decided by"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("architectural request")
        verbose_name_plural = _("architectural requests")
        ordering = ["-submitted_on", "-pk"]
        indexes = [models.Index(fields=["association", "status"], name="arc_request_status_idx")]

    def __str__(self) -> str:
        return f"{self.unit} · {self.title}"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN

    def is_overdue(self, today) -> bool:
        return (
            self.status in self.WAITING_ON_BOARD
            and self.decide_by is not None
            and today > self.decide_by
        )

    def answer_rows(self) -> list[tuple[str, object]]:
        return [(f.get("label", f["key"]), self.answers.get(f["key"])) for f in self.form_fields]


class ArcAttachment(AssociationScopedModel):
    """A file on a request, stored as a board-only library document."""

    request = models.ForeignKey(
        ArcRequest, on_delete=models.CASCADE, related_name="attachments", verbose_name=_("request")
    )
    version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("file"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("attachment")
        verbose_name_plural = _("attachments")
        ordering = ["pk"]


class ArcComment(AssociationScopedModel):
    request = models.ForeignKey(
        ArcRequest, on_delete=models.CASCADE, related_name="comments", verbose_name=_("request")
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("author"),
    )
    author_name = models.CharField(_("author name"), max_length=200, blank=True)
    body = models.TextField(_("comment"))
    internal = models.BooleanField(
        _("reviewers only"), default=True, help_text=_("Hidden from the requester.")
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("comment")
        verbose_name_plural = _("comments")
        ordering = ["created_at", "pk"]


class VoteChoice(models.TextChoices):
    APPROVE = "approve", _("Approve")
    APPROVE_WITH_CONDITIONS = "approve_with_conditions", _("Approve with conditions")
    DENY = "deny", _("Deny")
    ABSTAIN = "abstain", _("Abstain")


class ArcVote(AssociationScopedModel):
    request = models.ForeignKey(
        ArcRequest, on_delete=models.CASCADE, related_name="votes", verbose_name=_("request")
    )
    reviewer = models.ForeignKey(
        "registry.Person", on_delete=models.PROTECT, related_name="+", verbose_name=_("reviewer")
    )
    vote = models.CharField(_("vote"), max_length=30, choices=VoteChoice.choices)
    note = models.TextField(_("note"), blank=True)
    cast_at = models.DateTimeField(auto_now=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("architectural review vote")
        verbose_name_plural = _("architectural review votes")
        ordering = ["reviewer__full_name", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["request", "reviewer"], name="arc_vote_once")
        ]
