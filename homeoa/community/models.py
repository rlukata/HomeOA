"""Announcements (COM-01), calendar events and per-user iCal feed tokens (COM-06)."""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class Audience(models.TextChoices):
    """Who sees a post. Tenants see ``all`` and ``tenants`` only."""

    ALL = "all", _("Everyone")
    OWNERS = "owners", _("Owners")
    TENANTS = "tenants", _("Tenants")
    BOARD = "board", _("Board")


class Announcement(AssociationScopedModel):
    title = models.CharField(_("title"), max_length=200)
    body = models.TextField(_("message"), blank=True)
    audience = models.CharField(_("audience"), max_length=10, choices=Audience, default="all")
    publish_on = models.DateField(
        _("publish on"), help_text=_("Residents see it from this day (association time).")
    )
    expires_on = models.DateField(
        _("expires after"),
        null=True,
        blank=True,
        help_text=_("Last day it is shown. Leave empty to keep it up."),
    )
    pinned = models.BooleanField(_("pinned"), default=False)
    also_notify = models.BooleanField(
        _("also notify"),
        default=False,
        help_text=_("Send it to the audience: by email to those who consented, else in-app."),
    )
    notified_on = models.DateField(_("notified on"), null=True, blank=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("announcement")
        verbose_name_plural = _("announcements")
        ordering = ["-pinned", "-publish_on", "-pk"]

    def __str__(self) -> str:
        return self.title

    def clean(self):
        if self.expires_on and self.publish_on and self.expires_on < self.publish_on:
            raise ValidationError({"expires_on": _("Must be on or after the publish date.")})

    def is_live(self, today) -> bool:
        return self.publish_on <= today and (self.expires_on is None or self.expires_on >= today)

    def state(self, today) -> str:
        if self.publish_on > today:
            return "scheduled"
        if self.expires_on is not None and self.expires_on < today:
            return "expired"
        return "live"


class CalendarEvent(AssociationScopedModel):
    """A stored event. Times are wall-clock times in the association's time zone; an
    event with no start time is all-day."""

    MANUAL = "manual"

    title = models.CharField(_("title"), max_length=200)
    description = models.TextField(_("details"), blank=True)
    location = models.CharField(_("location"), max_length=200, blank=True)
    audience = models.CharField(_("audience"), max_length=10, choices=Audience, default="all")
    start_date = models.DateField(_("start date"))
    start_time = models.TimeField(
        _("start time"), null=True, blank=True, help_text=_("Leave empty for an all-day event.")
    )
    end_date = models.DateField(_("end date"), null=True, blank=True)
    end_time = models.TimeField(_("end time"), null=True, blank=True)
    # "manual", or the key of a registered source (services.register_source) that wrote it.
    source = models.CharField(_("source"), max_length=40, default=MANUAL, editable=False)
    source_ref = models.CharField(_("source reference"), max_length=100, blank=True, editable=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("calendar event")
        verbose_name_plural = _("calendar events")
        ordering = ["start_date", "start_time", "pk"]

    def __str__(self) -> str:
        return self.title

    @property
    def all_day(self) -> bool:
        return self.start_time is None

    def clean(self):
        if self.start_time is None and self.end_time is not None:
            raise ValidationError({"end_time": _("An all-day event has no end time.")})
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": _("Must be on or after the start date.")})
        if (
            self.start_time
            and self.end_time
            and (self.end_date or self.start_date) == self.start_date
            and self.end_time < self.start_time
        ):
            raise ValidationError({"end_time": _("Must be after the start time.")})


def new_token() -> str:
    return secrets.token_urlsafe(32)


class CalendarFeedToken(AssociationScopedModel):
    """The secret in a user's iCal URL. Revoking it kills the URL; a new one replaces it."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("user"),
    )
    token = models.CharField(_("token"), max_length=64, unique=True, default=new_token)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)

    class Meta:
        verbose_name = _("calendar feed token")
        verbose_name_plural = _("calendar feed tokens")
        constraints = [
            models.UniqueConstraint(
                fields=["association", "user"],
                condition=models.Q(revoked_at__isnull=True),
                name="community_one_live_feed_token",
            )
        ]

    def __str__(self) -> str:
        return f"feed {self.user_id}"


class Poll(AssociationScopedModel):
    """A non-binding question to owners (GOV-11): one answer per unit, not a vote."""

    question = models.CharField(_("question"), max_length=300)
    details = models.TextField(_("details"), blank=True)
    choices = models.JSONField(_("choices"), default=list)
    opens_on = models.DateField(_("opens on"))
    closes_on = models.DateField(_("closes on"))
    include_tenants = models.BooleanField(
        _("tenants answer too"),
        default=False,
        help_text=_("Off: owners only. On: each unit's owners or tenants answer, once per unit."),
    )
    show_results = models.BooleanField(_("show results to residents after it closes"), default=True)
    closed_early_on = models.DateField(_("closed early on"), null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("poll")
        verbose_name_plural = _("polls")
        ordering = ["-opens_on", "-pk"]

    def __str__(self) -> str:
        return self.question

    def is_open(self, on) -> bool:
        return self.closed_early_on is None and self.opens_on <= on <= self.closes_on

    def is_closed(self, on) -> bool:
        return self.closed_early_on is not None or on > self.closes_on


class PollAnswer(AssociationScopedModel):
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name="answers")
    unit = models.ForeignKey("registry.Unit", on_delete=models.PROTECT, related_name="+")
    person = models.ForeignKey(
        "registry.Person", on_delete=models.SET_NULL, null=True, related_name="+"
    )
    choice = models.PositiveSmallIntegerField(_("choice"))
    answered_at = models.DateTimeField(auto_now=True)

    class Meta(AssociationScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["poll", "unit"], name="community_poll_one_per_unit")
        ]
