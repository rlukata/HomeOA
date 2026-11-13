from django import forms
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.billing.models import Charge
from homeoa.ledger.models import Account
from homeoa.registry.models import Unit

from .models import Method


def _units(association):
    units = list(Unit.all_associations.filter(association=association))
    return sorted(
        units, key=lambda u: (0, int(u.number), "") if u.number.isdigit() else (1, 0, u.number)
    )


class UnitChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return _("Unit %(n)s") % {"n": obj.number}


class _Base(forms.Form):
    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.association = association
        if "unit" in self.fields:
            units = _units(association)
            self.fields["unit"].queryset = Unit.all_associations.filter(
                pk__in=[u.pk for u in units]
            )
            self.fields["unit"].choices = [("", "---------")] + [
                (u.pk, _("Unit %(n)s") % {"n": u.number}) for u in units
            ]


class ReceiptForm(_Base):
    unit = UnitChoiceField(label=_("Unit"), queryset=Unit.objects.none())
    amount = forms.DecimalField(label=_("Amount"), max_digits=14, decimal_places=2, min_value=0.01)
    received_date = forms.DateField(
        label=_("Received on"), widget=forms.DateInput(attrs={"type": "date"})
    )
    # ACH receipts arrive only through the payments plugin, never by hand.
    method = forms.ChoiceField(
        label=_("Method"), choices=[c for c in Method.choices if c[0] != Method.ACH]
    )
    reference = forms.CharField(
        label=_("Reference"),
        required=False,
        max_length=100,
        help_text=_("Check number or confirmation."),
    )
    payer = forms.CharField(label=_("Payer"), required=False, max_length=200)
    deposit_account = forms.ModelChoiceField(
        label=_("Deposited to"), queryset=Account.objects.none(), empty_label=None
    )
    memo = forms.CharField(label=_("Memo"), required=False, max_length=200)

    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, association=association, **kwargs)
        from homeoa.banking.models import BankAccount

        # Operating cash only: owners are billed in the operating fund.
        bank = BankAccount.all_associations.filter(
            association=association, fund__code="operating"
        ).values("gl_account")
        accounts = Account.all_associations.filter(association=association, active=True)
        self.fields["deposit_account"].queryset = accounts.filter(
            models.Q(code="1000") | models.Q(pk__in=bank)
        ).order_by("code")


class VoidForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), widget=forms.Textarea(attrs={"rows": 2}))


class RefundForm(forms.Form):
    amount = forms.DecimalField(
        label=_("Amount refunded"), max_digits=14, decimal_places=2, min_value=0.01
    )
    date = forms.DateField(label=_("Refunded on"), widget=forms.DateInput(attrs={"type": "date"}))
    method = forms.ChoiceField(label=_("How it was sent"), choices=Method.choices)
    reference = forms.CharField(label=_("Reference"), required=False, max_length=100)
    reason = forms.CharField(label=_("Reason"), widget=forms.Textarea(attrs={"rows": 2}))


class AdjustmentForm(_Base):
    KINDS = (
        ("credit_memo", _("Credit memo")),
        ("write_off", _("Write-off")),
        ("manual_charge", _("Manual charge")),
        ("charge_void", _("Void a charge")),
    )
    kind = forms.ChoiceField(label=_("Type"), choices=KINDS)
    unit = UnitChoiceField(label=_("Unit"), queryset=Unit.objects.none())
    amount = forms.DecimalField(
        label=_("Amount"), max_digits=14, decimal_places=2, min_value=0.01, required=False
    )
    date = forms.DateField(label=_("Date"), widget=forms.DateInput(attrs={"type": "date"}))
    charge = forms.ModelChoiceField(
        label=_("Charge"),
        queryset=Charge.objects.none(),
        required=False,
        help_text=_("Optional for a credit memo or write-off; required to void a charge."),
    )
    description = forms.CharField(
        label=_("Charge description"),
        required=False,
        max_length=200,
        help_text=_("For a manual charge, e.g. “Hallway damage repair”."),
    )
    reason = forms.CharField(label=_("Reason"), widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, association=association, **kwargs)
        self.fields["charge"].queryset = (
            Charge.all_associations.filter(association=association, void_entry__isnull=True)
            .select_related("unit")
            .order_by("-charge_date", "pk")
        )
        self.fields["charge"].label_from_instance = lambda c: (
            _("Unit %(u)s · %(d)s · open %(o)s")
            % {"u": c.unit.number, "d": c.description, "o": f"${c.open_balance:,.2f}"}
        )

    def clean(self):
        data = super().clean()
        kind, charge, unit = data.get("kind"), data.get("charge"), data.get("unit")
        if kind == "charge_void" and not charge:
            self.add_error("charge", _("Choose the charge to void."))
        if kind != "charge_void" and not data.get("amount"):
            self.add_error("amount", _("Enter an amount."))
        if charge and unit and charge.unit_id != unit.pk:
            self.add_error("charge", _("That charge belongs to another unit."))
        return data


class AutopaySettingsForm(forms.Form):
    """The autopay debit day and how long before it the notice goes out."""

    debit_day = forms.IntegerField(
        label=_("Debit day"),
        min_value=1,
        max_value=28,
        help_text=_("Day of the month autopay debits owners' bank accounts (1 to 28)."),
    )
    notice_lead_days = forms.IntegerField(
        label=_("Notice lead (days)"),
        min_value=1,
        help_text=_("How many days before the debit owners get the amount and the date."),
    )

    def __init__(self, *args, least: int = 10, most: int = 45, **kwargs):
        super().__init__(*args, **kwargs)
        self.least, self.most = least, most

    def clean_notice_lead_days(self):
        lead = self.cleaned_data["notice_lead_days"]
        if lead < self.least:
            raise forms.ValidationError(
                _("The notice must go out at least %(n)d days before the debit."),
                params={"n": self.least},
            )
        if lead > self.most:
            raise forms.ValidationError(
                _("The notice may go out at most %(n)d days before the debit."),
                params={"n": self.most},
            )
        return lead
