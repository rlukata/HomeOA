"""Roles and role assignments (PLT-03).

System roles take their permissions from ``permissions.DEFAULT_ROLES`` (so code updates
reach existing associations); custom roles store theirs in ``Role.permissions``.
Owner and tenant are derived from registry data and are never assigned by hand.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel

from . import permissions as perms


class Role(AssociationScopedModel):
    slug = models.SlugField(_("slug"), max_length=50)
    name = models.CharField(_("name"), max_length=100)
    is_system = models.BooleanField(_("system role"), default=False)
    permissions = models.JSONField(_("permissions"), default=list, blank=True)
    member_limit = models.PositiveSmallIntegerField(
        _("member limit"),
        null=True,
        blank=True,
        help_text=_("Most people who can hold this role at once (committees). Blank: no limit."),
    )

    class Meta:
        verbose_name = _("role")
        verbose_name_plural = _("roles")
        ordering = ["-is_system", "name", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["association", "slug"], name="access_role_slug_unique")
        ]

    def __str__(self) -> str:
        if self.is_system and self.slug in perms.DEFAULT_ROLES:
            return str(perms.DEFAULT_ROLES[self.slug]["name"])
        return str(self.name)

    @property
    def permission_set(self) -> frozenset[str]:
        if self.is_system and self.slug in perms.DEFAULT_ROLES:
            return perms.DEFAULT_ROLES[self.slug]["permissions"]
        return frozenset(c for c in (self.permissions or []) if c in perms.LABELS)

    @property
    def limit(self) -> int | None:
        if self.is_system and self.slug in perms.DEFAULT_ROLES:
            return perms.DEFAULT_ROLES[self.slug].get("member_limit")
        return self.member_limit

    @property
    def assignable(self) -> bool:
        return self.slug not in perms.DERIVED_ROLES

    def clean(self):
        unknown = set(self.permissions or []) - set(perms.ALL)
        if unknown:
            raise ValidationError(_("Unknown permissions: %(codes)s"), params={"codes": unknown})


class RoleAssignment(AssociationScopedModel):
    class Title(models.TextChoices):
        NONE = "", _("None")
        PRESIDENT = "president", _("President")
        VICE_PRESIDENT = "vice_president", _("Vice president")
        TREASURER = "treasurer", _("Treasurer")
        SECRETARY = "secretary", _("Secretary")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="role_assignments",
        verbose_name=_("user"),
    )
    role = models.ForeignKey(
        Role, on_delete=models.PROTECT, related_name="assignments", verbose_name=_("role")
    )
    title = models.CharField(
        _("officer title"), max_length=20, choices=Title, default=Title.NONE, blank=True
    )
    unit = models.ForeignKey(
        "registry.Unit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("unit scope"),
    )
    start = models.DateField(_("start date"))
    end = models.DateField(_("end date"), null=True, blank=True)

    class Meta:
        verbose_name = _("role assignment")
        verbose_name_plural = _("role assignments")
        ordering = ["role__name", "user__email", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end__isnull=True) | models.Q(end__gte=models.F("start")),
                name="access_assignment_dates",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} - {self.role}"

    def _overlapping(self, qs):
        """Assignments in ``qs`` (other than this one) whose dates overlap this one's."""
        qs = qs.exclude(pk=self.pk)
        if self.end:
            qs = qs.filter(start__lte=self.end)
        return qs.exclude(end__lt=self.start)

    def clean(self):
        super().clean()
        if not self.start:
            return
        mine = RoleAssignment.all_associations.filter(association_id=self.association_id)
        if self.title and self.role_id and self.role.slug != perms.BOARD:
            raise ValidationError(_("Officer titles are only for board members."))
        if self.title and self.user_id:
            other_titles = mine.filter(user_id=self.user_id).exclude(title="")
            if self._overlapping(other_titles).exists():
                raise ValidationError(
                    _(
                        "A person can hold only one officer title at a time. Change or end "
                        "their current title first."
                    )
                )
        limit = self.role.limit if self.role_id else None
        if limit:
            others = self._overlapping(mine.filter(role_id=self.role_id))
            holders = set(others.exclude(user_id=self.user_id).values_list("user_id", flat=True))
            if len(holders) >= limit:
                raise ValidationError(
                    _("%(role)s can have at most %(limit)s members at a time."),
                    params={"role": self.role, "limit": limit},
                )

    def is_active_on(self, day) -> bool:
        return self.start <= day and (self.end is None or self.end >= day)


class Invitation(AssociationScopedModel):
    """A resident invitation (POR-01). Drafted any time; sent only once residents are on."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENT = "sent", _("Sent")
        REVOKED = "revoked", _("Revoked")

    person = models.ForeignKey(
        "registry.Person",
        on_delete=models.CASCADE,
        related_name="invitations",
        verbose_name=_("person"),
    )
    email = models.EmailField(_("email"))
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("drafted by"),
    )
    created_at = models.DateTimeField(_("drafted at"), auto_now_add=True)
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("sent by"),
    )
    send_count = models.PositiveSmallIntegerField(_("times sent"), default=0)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("revoked by"),
    )

    class Meta:
        verbose_name = _("invitation")
        verbose_name_plural = _("invitations")
        ordering = ["person__full_name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "person"], name="access_invitation_person_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.person} <{self.email}>"

    @property
    def expires_at(self):
        """When a sent invitation goes stale (``sent_at`` + the association's expiry days)."""
        from .invitations import expiry_days

        if self.status != self.Status.SENT or self.sent_at is None:
            return None
        return self.sent_at + timedelta(days=expiry_days(self.association))

    @property
    def is_expired(self) -> bool:
        exp = self.expires_at
        return exp is not None and timezone.now() >= exp
