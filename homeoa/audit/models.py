from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.context import get_current_association
from homeoa.core.models import (
    Association,
    AssociationScopedManager,
    AssociationScopedModel,
    AssociationScopedQuerySet,
)


class AuditImmutableError(Exception):
    """Audit events are append-only."""


class AuditEventQuerySet(AssociationScopedQuerySet):
    def update(self, **kwargs):
        raise AuditImmutableError("Audit events cannot be updated.")

    def delete(self):
        raise AuditImmutableError("Audit events cannot be deleted.")

    update.queryset_only = True
    delete.queryset_only = True


class AuditEventManager(AssociationScopedManager.from_queryset(AuditEventQuerySet)):
    pass


class AuditEvent(AssociationScopedModel):
    """One immutable record of who did what, when.

    ``association`` may be empty for events outside any association (e.g. a failed
    login before one is resolved). ``changes`` maps field name to ``[before, after]``;
    encrypted fields show ``"changed"`` instead of values.
    """

    ACTION_CREATE = "create"
    ACTION_UPDATE = "update"
    ACTION_DELETE = "delete"

    association = models.ForeignKey(
        Association,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        verbose_name=_("association"),
    )
    created_at = models.DateTimeField(_("time"), auto_now_add=True, db_index=True)
    # No DB constraint and no cascade: deleting a user must never touch the log.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("actor"),
    )
    actor_label = models.CharField(_("actor name"), max_length=200, blank=True)
    action = models.CharField(_("action"), max_length=80, db_index=True)
    object_type = models.CharField(_("object type"), max_length=100, blank=True, db_index=True)
    object_id = models.CharField(_("object id"), max_length=64, blank=True)
    object_repr = models.CharField(_("object"), max_length=200, blank=True)
    changes = models.JSONField(_("changes"), default=dict, blank=True)
    message = models.TextField(_("message"), blank=True)
    ip = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    request_id = models.CharField(_("request id"), max_length=64, blank=True)

    objects = AuditEventManager()
    all_associations = AuditEventQuerySet.as_manager()

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("audit event")
        verbose_name_plural = _("audit events")
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["object_type", "object_id"])]

    def __str__(self) -> str:
        return f"{self.action} {self.object_type}:{self.object_id}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise AuditImmutableError("Audit events cannot be changed.")
        # Unlike other scoped models, an event without an association is allowed.
        if self.association_id is None:
            self.association = get_current_association()
        models.Model.save(self, *args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditImmutableError("Audit events cannot be deleted.")
