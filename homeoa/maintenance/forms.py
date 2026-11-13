from django import forms
from django.utils.translation import gettext_lazy as _

from homeoa.registry.models import Person, Unit
from homeoa.vendors.models import Vendor

from . import services
from .models import Component, MaintenanceRequest, PreventiveTask, WorkOrder

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiplePhotoField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault(
            "widget", MultipleFileInput(attrs={"accept": "image/png,image/jpeg", "multiple": True})
        )
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        if isinstance(data, list | tuple):
            files = [super(MultiplePhotoField, self).clean(d, initial) for d in data if d]
        else:
            files = [super().clean(data, initial)] if data else []
        for f in files:
            try:
                services.check_photo(f)
            except services.MaintenanceError as exc:
                raise forms.ValidationError(exc.messages) from exc
        if len(files) > services.MAX_PHOTOS:
            raise forms.ValidationError(
                _("Attach at most %(n)s photos.") % {"n": services.MAX_PHOTOS}
            )
        return files


class RequestForm(forms.ModelForm):
    """Board entry, usually on a resident's behalf."""

    photos = MultiplePhotoField(label=_("Photos"))

    class Meta:
        model = MaintenanceRequest
        fields = ["unit", "location", "title", "description", "priority", "submitted_by"]
        labels = {"submitted_by": _("On behalf of")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit"].queryset = Unit.objects.order_by("number")
        self.fields["unit"].empty_label = _("Common area")
        self.fields["submitted_by"].queryset = Person.objects.order_by("full_name")
        self.fields["submitted_by"].required = False


class ResidentRequestForm(forms.Form):
    """Owner or tenant submission; units limited to the user's own."""

    unit = forms.ChoiceField(label=_("Unit"), required=False)
    location = forms.CharField(
        label=_("Where is it?"), max_length=200, required=False, help_text=_("e.g. back deck")
    )
    title = forms.CharField(label=_("What needs fixing?"), max_length=200)
    description = forms.CharField(label=_("Details"), widget=forms.Textarea, required=False)
    priority = forms.ChoiceField(
        label=_("Priority"),
        choices=MaintenanceRequest.Priority.choices,
        initial=MaintenanceRequest.Priority.NORMAL,
    )
    photos = MultiplePhotoField(label=_("Photos"))

    def __init__(self, *args, units=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.units = {str(u.pk): u for u in units}
        self.fields["unit"].choices = [("", _("Common area"))] + [
            (str(u.pk), _("Unit %(n)s") % {"n": u.number}) for u in units
        ]
        if len(self.units) == 1:
            self.fields["unit"].initial = next(iter(self.units))

    def clean_unit(self):
        value = self.cleaned_data.get("unit")
        return self.units.get(value) if value else None


class RequestStatusForm(forms.Form):
    status = forms.ChoiceField(label=_("Status"), choices=MaintenanceRequest.Status.choices)
    note = forms.CharField(label=_("Note"), max_length=500, required=False)


class WorkOrderForm(forms.ModelForm):
    class Meta:
        model = WorkOrder
        fields = ["vendor", "scope", "estimated_cost", "scheduled_for"]
        widgets = {"scheduled_for": DATE}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Vendor.objects.filter(status=Vendor.Status.ACTIVE)
        if self.instance.vendor_id:
            qs = Vendor.objects.filter(pk=self.instance.vendor_id) | qs
        self.fields["vendor"].queryset = qs.order_by("name")
        self.fields["estimated_cost"].widget.attrs.update({"step": "0.01", "min": "0"})

    def clean_estimated_cost(self):
        value = self.cleaned_data.get("estimated_cost")
        if value is not None and value < 0:
            raise forms.ValidationError(_("Enter zero or more."))
        return value


class WorkOrderStatusForm(forms.Form):
    status = forms.ChoiceField(label=_("Status"), choices=WorkOrder.Status.choices)
    scheduled_for = forms.DateField(label=_("Scheduled for"), required=False, widget=DATE)
    note = forms.CharField(label=_("Note"), max_length=500, required=False)


class LinkBillForm(forms.Form):
    bill = forms.ModelChoiceField(label=_("Bill"), queryset=None)

    def __init__(self, *args, work_order, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bill"].queryset = services.linkable_bills(work_order)
        self.fields["bill"].label_from_instance = lambda b: (
            f"#{b.invoice_number} · {b.bill_date:%Y-%m-%d} · ${b.total:,.2f}"
        )


class ComponentForm(forms.ModelForm):
    class Meta:
        model = Component
        fields = [
            "name",
            "category",
            "location",
            "installed_on",
            "useful_life_years",
            "condition",
            "replacement_cost",
            "cost_as_of",
            "notes",
            "active",
        ]
        widgets = {
            "installed_on": DATE,
            "cost_as_of": DATE,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class PreventiveTaskForm(forms.ModelForm):
    class Meta:
        model = PreventiveTask
        fields = [
            "title",
            "scope",
            "component",
            "vendor",
            "every_months",
            "next_due",
            "lead_days",
            "estimated_cost",
            "active",
        ]
        widgets = {"next_due": DATE, "scope": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Vendor.objects.filter(status=Vendor.Status.ACTIVE)
        if self.instance.vendor_id:
            qs = Vendor.objects.filter(pk=self.instance.vendor_id) | qs
        self.fields["vendor"].queryset = qs.order_by("name")
        self.fields["component"].queryset = Component.objects.filter(active=True)
