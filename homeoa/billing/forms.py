from django import forms
from django.utils.translation import gettext_lazy as _

from homeoa.changes.forms import add_resolution_field
from homeoa.ledger.models import Account, Fund

from .models import AssessmentSchedule


class ScheduleForm(forms.ModelForm):
    class Meta:
        model = AssessmentSchedule
        fields = [
            "name",
            "method",
            "annual_amount",
            "period_amount",
            "type_amounts",
            "unit_types",
            "reserve_annual_amount",
            "receivable_account",
            "income_account",
            "operating_fund",
            "reserve_fund",
            "reserve_expense_account",
            "due_to_reserve_account",
            "due_from_operating_account",
            "reserve_income_account",
            "insurance_annual_amount",
            "insurance_fund",
            "insurance_expense_account",
            "due_to_insurance_account",
            "insurance_due_from_operating_account",
            "insurance_income_account",
            "post_day",
            "due_day",
            "effective_start",
            "effective_end",
            "active",
        ]
        widgets = {
            "effective_start": forms.DateInput(attrs={"type": "date"}),
            "effective_end": forms.DateInput(attrs={"type": "date"}),
            "type_amounts": forms.Textarea(attrs={"rows": 2}),
            "unit_types": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.association = association or self.instance.association
        # Dues changes are approved by a board resolution.
        add_resolution_field(self, association)
        accounts = Account.all_associations.filter(association=association, active=True)
        funds = Fund.all_associations.filter(association=association, active=True)
        self.fields["receivable_account"].queryset = accounts.filter(in_unit_subledger=True)
        self.fields["income_account"].queryset = accounts.filter(type="income")
        self.fields["operating_fund"].queryset = funds
        self.fields["reserve_fund"].queryset = funds
        self.fields["insurance_fund"].queryset = funds
        for name in (
            "reserve_expense_account",
            "due_to_reserve_account",
            "due_from_operating_account",
            "reserve_income_account",
            "insurance_expense_account",
            "due_to_insurance_account",
            "insurance_due_from_operating_account",
            "insurance_income_account",
        ):
            self.fields[name].queryset = accounts


class RunForm(forms.Form):
    schedule = forms.ModelChoiceField(
        label=_("Schedule"), queryset=AssessmentSchedule.objects.none(), empty_label=None
    )
    period = forms.CharField(
        label=_("Month"),
        widget=forms.DateInput(attrs={"type": "month"}),
        help_text=_("The month to bill, e.g. 2026-10."),
    )

    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["schedule"].queryset = AssessmentSchedule.all_associations.filter(
            association=association, active=True
        )

    def clean_period(self):
        from .services import BillingError, parse_period

        try:
            return parse_period(self.cleaned_data["period"].strip())
        except BillingError as exc:
            raise forms.ValidationError(str(exc)) from None
