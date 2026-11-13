"""The "board resolution" field on screens whose changes a resolution approves."""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

FIELD = "change_resolution"


def add_resolution_field(form: forms.Form, association) -> None:
    """Add a required resolution picker: draft or adopted resolutions not backing another
    change. While an approved change is being applied, its own resolution is accepted."""
    from homeoa.governance.models import Resolution

    from . import replay, services

    if replay.is_replaying():
        queryset = Resolution.all_associations.filter(
            association=association, status=Resolution.Status.ADOPTED
        )
    else:
        queryset = services.usable_resolutions(association)
    field = forms.ModelChoiceField(
        label=_("Board resolution"),
        queryset=queryset,
        help_text=_(
            "The resolution that approves this change. It takes effect when the resolution is "
            "adopted (at once if it already is). Draft one in Governance > Board & committees."
        ),
    )
    field.label_from_instance = lambda r: f"{r.number} · {r.title} ({r.get_status_display()})"
    form.fields[FIELD] = field
