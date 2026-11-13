from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import ENGLISH, NoticePreference, Occupancy, Person, Unit

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class UnitForm(forms.ModelForm):
    class Meta:
        model = Unit
        fields = [
            "number",
            "building",
            "allocated_interest",
            "vote_weight",
            "parcel_id",
            "address_line1",
            "address_line2",
            "city",
            "region",
            "postal_code",
            "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class PersonForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = [
            "full_name",
            "preferred_name",
            "email",
            "alt_email",
            "phone",
            "alt_phone",
            "address_line1",
            "address_line2",
            "city",
            "region",
            "postal_code",
            "preferred_language",
            "phone_unlisted",
            "email_confidential",
            "acp_participant",
            "acp_address",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "acp_address": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["preferred_language"].required = False

    def clean_preferred_language(self):
        return self.cleaned_data.get("preferred_language") or ENGLISH


class TransferForm(forms.Form):
    new_owner_name = forms.CharField(label=_("New owner name"), max_length=200)
    new_owner_email = forms.EmailField(label=_("New owner email"), required=False)
    second_owner_name = forms.CharField(
        label=_("Second owner name (optional)"), max_length=200, required=False
    )
    second_owner_email = forms.EmailField(label=_("Second owner email"), required=False)
    effective_date = forms.DateField(label=_("Effective date"), widget=DATE)
    notes = forms.CharField(label=_("Notes"), max_length=200, required=False)


class OccupancyForm(forms.Form):
    name = forms.CharField(label=_("Name"), max_length=200)
    email = forms.EmailField(label=_("Email"), required=False)
    phone = forms.CharField(label=_("Phone"), max_length=40, required=False)
    kind = forms.ChoiceField(label=_("Kind"), choices=Occupancy.Kind.choices)
    start = forms.DateField(label=_("Start date"), widget=DATE)
    end = forms.DateField(label=_("End date"), widget=DATE, required=False)
    lease_document = forms.FileField(label=_("Lease"), required=False)


class EndOccupancyForm(forms.Form):
    end = forms.DateField(label=_("End date"), widget=DATE)


EVIDENCE_TYPES = ("application/pdf", "image/jpeg", "image/png")
EVIDENCE_MAX_BYTES = 10 * 1024 * 1024


class NoticePreferenceForm(forms.Form):
    """Board entry: email notices only with a signed paper consent and its scan."""

    delivery = forms.ChoiceField(label=_("Notices by"), choices=NoticePreference.Delivery.choices)
    source = forms.ChoiceField(
        label=_("Source"),
        choices=[
            (NoticePreference.Source.PAPER, NoticePreference.Source.PAPER.label),
            (NoticePreference.Source.BOARD, NoticePreference.Source.BOARD.label),
        ],
    )
    evidence = forms.FileField(
        label=_("Scan of the signed consent form"),
        required=False,
        help_text=_("PDF, JPEG or PNG. Required to record email notices."),
    )
    note = forms.CharField(label=_("Note"), max_length=200, required=False)

    def clean(self):
        data = super().clean()
        email = data.get("delivery") == NoticePreference.Delivery.EMAIL
        evidence = data.get("evidence")
        if email and data.get("source") != NoticePreference.Source.PAPER:
            self.add_error(
                "source",
                _("The board can record email notices only from a signed paper consent form."),
            )
        if email and not evidence:
            self.add_error("evidence", _("Upload the scan of the signed consent form."))
        if evidence is not None:
            if evidence.content_type not in EVIDENCE_TYPES:
                self.add_error("evidence", _("Upload a PDF, JPEG or PNG file."))
            elif evidence.size > EVIDENCE_MAX_BYTES:
                self.add_error("evidence", _("The file is larger than 10 MB."))
        return data


class InterestsForm(forms.Form):
    """One allocated-interest field per unit; the total is checked on save, not per row."""

    save_anyway = forms.BooleanField(
        label=_("Save even though the total is not 100%"), required=False
    )

    def __init__(self, *args, units, **kwargs):
        super().__init__(*args, **kwargs)
        self.units = list(units)
        for unit in self.units:
            self.fields[f"unit_{unit.pk}"] = forms.DecimalField(
                label=str(unit),
                max_digits=7,
                decimal_places=4,
                min_value=Decimal("0"),
                max_value=Decimal("100"),
                initial=unit.allocated_interest,
            )

    def unit_fields(self):
        return [(unit, self[f"unit_{unit.pk}"]) for unit in self.units]

    def total(self) -> Decimal:
        return sum(
            (self.cleaned_data.get(f"unit_{u.pk}") or Decimal("0") for u in self.units),
            Decimal("0"),
        )


class RecordsFullExportForm(forms.Form):
    """Full (unredacted) owner records export: board only, reason required."""

    format = forms.ChoiceField(
        label=_("Format"), choices=[("csv", "CSV"), ("xlsx", "XLSX"), ("pdf", "PDF")]
    )
    reason = forms.CharField(
        label=_("Reason"),
        max_length=300,
        help_text=_("Recorded in the audit log, e.g. hand-over to a new manager."),
    )

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if not reason:
            raise forms.ValidationError(_("Give a reason."))
        return reason


class RecordsRequestForm(forms.Form):
    person = forms.ModelChoiceField(
        label=_("Owner"), queryset=Person.objects.none(), required=False
    )
    requester_name = forms.CharField(
        label=_("Or name (if not in the registry)"), max_length=200, required=False
    )
    received_on = forms.DateField(label=_("Received on"), widget=DATE)
    note = forms.CharField(label=_("What was asked for"), max_length=500, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = Person.objects.all()

    def clean(self):
        data = super().clean()
        if not data.get("person") and not (data.get("requester_name") or "").strip():
            raise forms.ValidationError(_("Pick the owner or enter a name."))
        return data
