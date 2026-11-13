from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils.translation import gettext_lazy as _

from homeoa.banking.models import BankAccount
from homeoa.ledger.models import Account, AccountType, Fund
from homeoa.vendors.models import Vendor

from .models import Bill, BillLine

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
MAX_UPLOAD = 20 * 1024 * 1024
ALLOWED_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp")


def check_upload(upload):
    if upload:
        if upload.size > MAX_UPLOAD:
            raise forms.ValidationError(_("The file is larger than 20 MB."))
        if getattr(upload, "content_type", "") not in ALLOWED_TYPES:
            raise forms.ValidationError(_("Upload a PDF or an image."))
    return upload


class AttachmentForm(forms.Form):
    attachment = forms.FileField(label=_("Invoice file (PDF or image)"))

    def clean_attachment(self):
        return check_upload(self.cleaned_data.get("attachment"))


class BillForm(forms.ModelForm):
    attachment = forms.FileField(
        label=_("Invoice file (PDF or image)"),
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": ",".join(ALLOWED_TYPES)}),
    )

    class Meta:
        model = Bill
        fields = ["vendor", "invoice_number", "bill_date", "due_date", "description"]
        widgets = {"bill_date": DATE, "due_date": DATE}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Vendor.objects.filter(status=Vendor.Status.ACTIVE)
        if self.instance.vendor_id:
            qs = Vendor.objects.filter(pk=self.instance.vendor_id) | qs
        self.fields["vendor"].queryset = qs.order_by("name")

    def clean_attachment(self):
        return check_upload(self.cleaned_data.get("attachment"))

    def clean(self):
        data = super().clean()
        vendor, number = data.get("vendor"), data.get("invoice_number")
        bill_date, due = data.get("bill_date"), data.get("due_date")
        if vendor and number:
            dupes = Bill.objects.filter(vendor=vendor, invoice_number__iexact=number.strip())
            dupes = dupes.exclude(status=Bill.Status.VOID).exclude(pk=self.instance.pk)
            if dupes.exists():
                self.add_error(
                    "invoice_number",
                    _("This vendor already has a bill with invoice number %(n)s.") % {"n": number},
                )
        if bill_date and due and due < bill_date:
            self.add_error("due_date", _("The due date cannot be before the bill date."))
        return data

    def clean_invoice_number(self):
        return self.cleaned_data["invoice_number"].strip()


class BillLineForm(forms.ModelForm):
    class Meta:
        model = BillLine
        fields = ["account", "fund", "amount", "memo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(
            type=AccountType.EXPENSE, active=True
        ).order_by("code")
        self.fields["account"].label_from_instance = lambda a: f"{a.code} · {a.name}"
        self.fields["fund"].queryset = Fund.objects.filter(active=True).order_by("sort_order")
        self.fields["amount"].min_value = None
        self.fields["amount"].widget.attrs.update({"step": "0.01", "min": "0.01"})

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is not None and amount <= 0:
            raise forms.ValidationError(_("Enter an amount greater than zero."))
        return amount


class BaseLineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        kept = [f for f in self.forms if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        if not kept:
            raise forms.ValidationError(_("Add at least one line."))


LineFormSet = inlineformset_factory(
    Bill, BillLine, form=BillLineForm, formset=BaseLineFormSet, extra=3, can_delete=True
)


def line_formset(lines: int = 0):
    """The lines formset with room for ``lines`` filled-in lines (at least the usual 3)."""
    if lines <= 3:
        return LineFormSet
    return inlineformset_factory(
        Bill, BillLine, form=BillLineForm, formset=BaseLineFormSet, extra=lines, can_delete=True
    )


class VoidForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), max_length=255)


class MarkPaidForm(forms.Form):
    paid_on = forms.DateField(label=_("Paid on"), widget=DATE)
    paid_by = forms.ChoiceField(
        label=_("Paid by"),
        choices=Bill.PaidBy.choices,
        initial=Bill.PaidBy.BANK,
        required=False,
        help_text=_(
            "Card and payment-app payments are reported on 1099-K by the processor, so "
            "they are left out of the vendor 1099 totals."
        ),
    )

    def __init__(self, *args, funds=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.funds = list(funds)
        for fund in self.funds:
            qs = BankAccount.objects.filter(fund=fund, active=True).select_related("gl_account")
            field = forms.ModelChoiceField(
                queryset=qs,
                label=_("Paid from (%(fund)s fund)") % {"fund": fund.name},
            )
            if qs.count() == 1:
                field.initial = qs.first().pk
            self.fields[f"bank_{fund.pk}"] = field

    def bank_accounts(self) -> dict:
        return {f.pk: self.cleaned_data[f"bank_{f.pk}"] for f in self.funds}
