"""Django form generated from a plugin's pydantic settings model."""

from __future__ import annotations

import typing
from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _
from pydantic_core import PydanticUndefined

from .manifest import PluginManifest


def _unwrap_optional(annotation):
    args = typing.get_args(annotation)
    if args and type(None) in args:
        rest = [a for a in args if a is not type(None)]
        if len(rest) == 1:
            return rest[0], True
    return annotation, False


def _form_field(name, info, *, secret: bool, secret_set: bool) -> forms.Field:
    annotation, optional = _unwrap_optional(info.annotation)
    has_default = info.default is not PydanticUndefined or info.default_factory is not None
    required = not optional and not has_default
    label = info.title or name.replace("_", " ").capitalize()
    kwargs = {"label": label, "help_text": info.description or ""}
    if secret:
        help_text = _("Saved. Leave blank to keep the current value.") if secret_set else ""
        return forms.CharField(
            label=label,
            required=required and not secret_set,
            widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
            help_text=help_text or info.description or "",
        )
    if typing.get_origin(annotation) is typing.Literal:
        choices = [(c, c) for c in typing.get_args(annotation)]
        return forms.ChoiceField(choices=choices, required=required, **kwargs)
    if annotation is bool:
        return forms.BooleanField(required=False, **kwargs)
    if annotation is int:
        return forms.IntegerField(required=required, **kwargs)
    if annotation is Decimal:
        return forms.DecimalField(required=required, **kwargs)
    return forms.CharField(required=required, **kwargs)


class PluginSettingsForm(forms.Form):
    enabled = forms.BooleanField(label=_("Enabled for this association"), required=False)

    def __init__(self, manifest: PluginManifest, installation=None, *args, **kwargs):
        initial = {"enabled": bool(installation and installation.enabled)}
        current = dict(installation.settings) if installation else {}
        secrets = dict(installation.secrets or {}) if installation else {}
        model = manifest.settings_model
        if model is not None:
            for name, info in model.model_fields.items():
                if name not in manifest.secret_fields:
                    if name in current:
                        initial[name] = current[name]
                    elif info.default is not PydanticUndefined:
                        initial[name] = info.default
        kwargs.setdefault("initial", initial)
        super().__init__(*args, **kwargs)
        self.manifest = manifest
        #: Fields marked ``json_schema_extra={"hidden": True}`` are settable only through
        #: ``save_installation`` (no board UI); a form save keeps their stored value.
        self.hidden_values = {}
        if model is not None:
            for name, info in model.model_fields.items():
                if (info.json_schema_extra or {}).get("hidden"):
                    if name in current:
                        self.hidden_values[name] = current[name]
                    continue
                secret = name in manifest.secret_fields
                self.fields[name] = _form_field(
                    name, info, secret=secret, secret_set=secret and name in secrets
                )

    def settings_data(self) -> dict:
        data = {k: v for k, v in self.cleaned_data.items() if k != "enabled"}
        data.update(self.hidden_values)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in data.items()}
