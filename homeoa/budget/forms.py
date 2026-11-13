from django import forms
from django.utils.translation import gettext_lazy as _

from homeoa.ledger.models import Account, Fund

from .models import Budget, BudgetLine


class NewBudgetForm(forms.Form):
    year = forms.IntegerField(label=_("Fiscal year"), min_value=2000, max_value=2100)
    name = forms.CharField(label=_("Name"), max_length=100, required=False)
    copy_from = forms.ModelChoiceField(
        label=_("Start from"),
        queryset=Budget.objects.none(),
        required=False,
        empty_label=_("Blank budget"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["copy_from"].queryset = Budget.objects.all()


class LineForm(forms.ModelForm):
    class Meta:
        model = BudgetLine
        fields = ["group", "label", "account", "fund", "amount", "note", "sort_order"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(
            active=True, type__in=["income", "expense"]
        )
        self.fields["fund"].queryset = Fund.objects.filter(active=True)


LineFormSet = forms.inlineformset_factory(
    Budget, BudgetLine, form=LineForm, extra=3, can_delete=True
)
