"""Test-only concrete model for exercising AssociationScopedModel (no migrations)."""

from django.db import models

from homeoa.core.crypto import EncryptedJSONField, EncryptedTextField
from homeoa.core.fields import MoneyField
from homeoa.core.models import AssociationScopedModel


class Widget(AssociationScopedModel):
    name = models.CharField(max_length=50)
    amount = MoneyField(default=0)

    class Meta(AssociationScopedModel.Meta):
        app_label = "scoped"


class Secret(AssociationScopedModel):
    """Test-only model with encrypted fields (homeoa.core.crypto, homeoa.audit)."""

    label = models.CharField(max_length=50)
    note = EncryptedTextField(blank=True, null=True)
    data = EncryptedJSONField(null=True, blank=True)
    hidden = models.CharField(max_length=50, blank=True)

    class Meta(AssociationScopedModel.Meta):
        app_label = "scoped"
