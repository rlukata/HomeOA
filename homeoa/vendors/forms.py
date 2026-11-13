from django import forms
from django.utils.translation import gettext_lazy as _

from homeoa.ledger.models import Account, AccountType

from .models import Contract, Vendor, VendorContact, VendorDocument, mask_account, mask_tin

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class VendorForm(forms.ModelForm):
    """Secrets (TIN, account number) are write-only: never prefilled, blank keeps the old one."""

    tin_input = forms.CharField(
        label=_("Taxpayer ID number"),
        required=False,
        max_length=20,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}, render_value=False),
    )
    account_input = forms.CharField(
        label=_("Our account number with the vendor"),
        required=False,
        max_length=60,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}, render_value=False),
    )
    w9_file = forms.FileField(label=_("W-9 (PDF)"), required=False)

    class Meta:
        model = Vendor
        fields = [
            "name",
            "category",
            "default_expense_account",
            "status",
            "notes",
            "legal_name",
            "tax_classification",
            "tin_type",
            "w9_received",
            "is_1099_eligible",
            "payee_name",
            "remit_address_line1",
            "remit_address_line2",
            "remit_city",
            "remit_region",
            "remit_postal_code",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3}), "w9_received": DATE}

    field_order = [
        *Meta.fields[:8],
        "tin_input",
        "w9_received",
        "w9_file",
        "is_1099_eligible",
        *Meta.fields[10:],
        "account_input",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_expense_account"].queryset = Account.objects.filter(
            type=AccountType.EXPENSE, active=True
        ).order_by("code")
        if self.instance.pk:
            current = mask_tin(self.instance.tin, self.instance.tin_type)
            if current:
                self.fields["tin_input"].help_text = _(
                    "On file: %(masked)s. Leave blank to keep it."
                ) % {"masked": current}
            acct = mask_account(self.instance.account_number)
            if acct:
                self.fields["account_input"].help_text = _(
                    "On file: %(masked)s. Leave blank to keep it."
                ) % {"masked": acct}

    def clean_tin_input(self):
        value = self.cleaned_data["tin_input"].strip()
        if value:
            digits = "".join(c for c in value if c.isdigit())
            if len(digits) != 9 or any(not (c.isdigit() or c in "- ") for c in value):
                raise forms.ValidationError(_("Enter the 9-digit SSN or EIN."))
            return digits
        return ""

    def clean_w9_file(self):
        upload = self.cleaned_data.get("w9_file")
        if upload and not (
            upload.name.lower().endswith(".pdf")
            or getattr(upload, "content_type", "") == "application/pdf"
        ):
            raise forms.ValidationError(_("Upload the W-9 as a PDF."))
        return upload

    def save(self, commit=True):
        vendor = super().save(commit=False)
        if self.cleaned_data.get("tin_input"):
            vendor.tin = self.cleaned_data["tin_input"]
        if self.cleaned_data.get("account_input", "").strip():
            vendor.account_number = self.cleaned_data["account_input"].strip()
        return vendor


class ContactForm(forms.ModelForm):
    class Meta:
        model = VendorContact
        fields = ["name", "role", "email", "phone", "is_primary"]


class VendorDocumentForm(forms.ModelForm):
    upload = forms.FileField(label=_("Certificate or license file"), required=False)

    class Meta:
        model = VendorDocument
        fields = ["kind", "issuer", "number", "effective_date", "expiry_date", "notes"]
        widgets = {
            "effective_date": DATE,
            "expiry_date": DATE,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def clean(self):
        data = super().clean()
        start, end = data.get("effective_date"), data.get("expiry_date")
        if start and end and end < start:
            self.add_error("expiry_date", _("The expiry date is before the effective date."))
        return data


class ContractForm(forms.ModelForm):
    upload = forms.FileField(label=_("Contract file"), required=False)

    class Meta:
        model = Contract
        fields = ["title", "start_date", "end_date", "auto_renew", "notice_days", "amount", "notes"]
        widgets = {
            "start_date": DATE,
            "end_date": DATE,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def clean(self):
        data = super().clean()
        start, end = data.get("start_date"), data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", _("The term ends before it starts."))
        return data
