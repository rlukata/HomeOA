from django import forms

from homeoa.documents.models import Category, Document

from .models import MasterPolicy

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class PolicyForm(forms.ModelForm):
    class Meta:
        model = MasterPolicy
        fields = [
            "carrier",
            "policy_number",
            "start_date",
            "end_date",
            "deductible",
            "property_limit",
            "liability_limit",
            "premium",
            "agent_name",
            "agent_email",
            "agent_phone",
            "certificate",
            "notes",
        ]
        widgets = {"start_date": DATE, "end_date": DATE, "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["certificate"].queryset = Document.all_associations.filter(
            association=association, category=Category.INSURANCE
        ).order_by("title")
