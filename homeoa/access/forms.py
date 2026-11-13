from django import forms
from django.utils.translation import gettext_lazy as _

from . import permissions as perms
from .models import Role, RoleAssignment


class AssignmentEditForm(forms.Form):
    """An assignment's officer title and dates. Editing changes the row in place; the audit
    log keeps the history."""

    title = forms.ChoiceField(
        label=_("Officer title"), choices=RoleAssignment.Title.choices, required=False
    )
    start = forms.DateField(label=_("Start date"), widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(
        label=_("End date"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def clean(self):
        data = super().clean()
        if data.get("end") and data.get("start") and data["end"] < data["start"]:
            self.add_error("end", _("The end date must be on or after the start date."))
        return data


class AssignmentForm(AssignmentEditForm):
    email = forms.EmailField(label=_("Email"))
    role = forms.ModelChoiceField(label=_("Role"), queryset=Role.objects.none())
    field_order = ["email", "role", "title", "start", "end"]

    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, **kwargs)
        roles = Role.all_associations.filter(association=association).exclude(
            slug__in=perms.DERIVED_ROLES
        )
        self.fields["role"].queryset = roles


class CustomRoleForm(forms.Form):
    name = forms.CharField(label=_("Role name"), max_length=100)
    permissions = forms.MultipleChoiceField(
        label=_("Permissions"),
        choices=[(c, label) for c, label in perms.LABELS.items() if c != perms.INSTANCE_MANAGE],
        widget=forms.CheckboxSelectMultiple,
    )
    member_limit = forms.IntegerField(
        label=_("Member limit"),
        min_value=1,
        required=False,
        help_text=_("For a committee: the most people who can hold the role at once."),
    )


class ResidentPauseForm(forms.Form):
    """Pause resident access, or lift the pause. Which one is decided by the current state."""

    reason = forms.CharField(
        label=_("Reason"),
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Saved in the audit log."),
    )
    confirm = forms.BooleanField(label=_("I understand who this affects."))


class GoLiveForm(forms.Form):
    """Confirm going live today, which cannot be undone."""

    confirm = forms.BooleanField(
        label=_("I understand that going live starts today and cannot be undone.")
    )


class SecurityForm(forms.Form):
    require_mfa_for_board = forms.BooleanField(
        label=_("Require MFA for board roles"), required=False
    )
