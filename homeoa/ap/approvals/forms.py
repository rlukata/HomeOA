"""Board form for editing approval rules ."""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from homeoa.access import permissions as perms
from homeoa.access.models import Role

from .models import ApprovalRule


class RejectForm(forms.Form):
    reason = forms.CharField(label=_("Reason for rejecting"), max_length=255)


class ApprovalRuleForm(forms.ModelForm):
    eligible_roles = forms.MultipleChoiceField(
        label=_("Eligible roles"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Leave empty for board members."),
    )

    class Meta:
        model = ApprovalRule
        fields = [
            "name",
            "priority",
            "active",
            "min_amount",
            "max_amount",
            "category",
            "vendor",
            "required_approvals",
            "eligible_roles",
            "allow_submitter",
        ]

    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, **kwargs)
        association = association or getattr(self.instance, "association", None)
        roles = Role.all_associations.filter(association=association).exclude(
            slug__in=perms.DERIVED_ROLES
        )
        self.fields["eligible_roles"].choices = [(r.slug, str(r)) for r in roles]
        if "vendor" in self.fields:
            from homeoa.vendors.models import Vendor

            self.fields["vendor"].queryset = Vendor.all_associations.filter(association=association)
        if association is not None and not self.instance.association_id:
            self.instance.association = association
