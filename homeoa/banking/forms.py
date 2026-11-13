from django import forms
from django.utils.translation import gettext_lazy as _

from homeoa.ledger.models import Account, AccountType, Fund

from .models import BankAccount, CategorizeRule, CsvColumnMapping, SignConvention, TxnStatus

MAX_UPLOAD = 5 * 1024 * 1024


class BankAccountForm(forms.ModelForm):
    account_number = forms.CharField(
        label=_("Account number"),
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
        help_text=_("Stored encrypted. Leave blank to keep the current number."),
    )

    class Meta:
        model = BankAccount
        fields = ["name", "institution", "account_number", "gl_account", "fund", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["account_number"] = ""
        self.fields["gl_account"].queryset = Account.objects.filter(
            type=AccountType.ASSET, active=True
        )
        self.fields["fund"].queryset = Fund.objects.filter(active=True)

    def clean_account_number(self):
        value = self.cleaned_data["account_number"].strip()
        if not value and self.instance.pk:
            return self.instance.account_number
        if value and not any(c.isdigit() for c in value):
            raise forms.ValidationError(_("Enter the account number's digits."))
        return value


class CsvMappingForm(forms.ModelForm):
    class Meta:
        model = CsvColumnMapping
        fields = [
            "date_column",
            "description_column",
            "amount_column",
            "sign",
            "debit_column",
            "credit_column",
            "payee_column",
            "memo_column",
            "reference_column",
            "balance_column",
            "date_format",
            "skip_rows",
        ]

    def clean(self):
        data = super().clean()
        if data.get("sign") == SignConvention.SPLIT:
            if not (data.get("debit_column") and data.get("credit_column")):
                raise forms.ValidationError(_("Name both the debit and credit columns."))
        elif not data.get("amount_column"):
            self.add_error("amount_column", _("Name the amount column."))
        return data


class ImportForm(forms.Form):
    bank_account = forms.ModelChoiceField(
        label=_("Bank account"), queryset=BankAccount.objects.none(), empty_label=None
    )
    file = forms.FileField(
        label=_("Statement file"),
        help_text=_("OFX, QFX or CSV downloaded from your bank. Up to 5 MB."),
        widget=forms.ClearableFileInput(attrs={"accept": ".ofx,.qfx,.csv,text/csv"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bank_account"].queryset = BankAccount.objects.filter(active=True)

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > MAX_UPLOAD:
            raise forms.ValidationError(_("The file is larger than 5 MB."))
        if f.size == 0:
            raise forms.ValidationError(_("The file is empty."))
        return f


class TxnFilterForm(forms.Form):
    account = forms.ModelChoiceField(
        label=_("Account"),
        queryset=BankAccount.objects.none(),
        required=False,
        empty_label=_("All accounts"),
    )
    status = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=[("", _("Any status")), *TxnStatus.choices],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = BankAccount.objects.all()


# ------------------------------------------------------------- matching


class AccountNameChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.name


def _income_expense():
    return Account.objects.filter(
        type__in=[AccountType.INCOME, AccountType.EXPENSE], active=True
    ).order_by("code")


class CategorizeForm(forms.Form):
    account = AccountNameChoice(label=_("Account"), queryset=Account.objects.none())
    save_rule = forms.CharField(
        label=_("Next time, categorize descriptions containing"),
        required=False,
        max_length=100,
        help_text=_("Optional. Saves a rule so similar transactions get this suggestion."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = _income_expense()


class CategorizeRuleForm(forms.ModelForm):
    account = AccountNameChoice(label=_("Account"), queryset=Account.objects.none())

    class Meta:
        model = CategorizeRule
        fields = ["contains", "account", "note", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = _income_expense()


class ReconcileStartForm(forms.Form):
    bank_account = forms.ModelChoiceField(
        label=_("Bank account"), queryset=BankAccount.objects.none(), empty_label=None
    )
    statement_date = forms.DateField(
        label=_("Statement date"), widget=forms.DateInput(attrs={"type": "date"})
    )
    ending_balance = forms.DecimalField(label=_("Ending balance"), max_digits=14, decimal_places=2)
    beginning_balance = forms.DecimalField(
        label=_("Beginning balance"),
        max_digits=14,
        decimal_places=2,
        required=False,
        help_text=_(
            "First reconciliation only. Leave blank to use the books' cash balance; later "
            "reconciliations start from the last ending balance."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bank_account"].queryset = BankAccount.objects.filter(active=True)


class ReserveApprovalForm(forms.Form):
    """Ask the board to approve money leaving a reserve bank account (``banking.reserve``).
    With ``txn`` the debit already happened and the amount is the debit's."""

    amount = forms.DecimalField(
        label=_("Amount"), max_digits=12, decimal_places=2, min_value=0.01, required=False
    )
    purpose = forms.CharField(
        label=_("Purpose"),
        max_length=300,
        help_text=_("What the money is for, e.g. “Roof repair, reserve component R-03”."),
    )
    txn = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, txn=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.txn = txn
        if txn is not None:
            self.fields["amount"].disabled = True
            self.initial["amount"] = -txn.amount
            self.initial["txn"] = txn.pk

    def clean(self):
        data = super().clean()
        if self.txn is not None:
            data["amount"] = -self.txn.amount
        elif not data.get("amount"):
            self.add_error("amount", _("Enter the amount."))
        return data
