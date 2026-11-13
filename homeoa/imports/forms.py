from django import forms
from django.utils.translation import gettext_lazy as _

from .models import ImportRun, Source

MAX_UPLOAD = 10 * 1024 * 1024


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": ".csv,text/csv"}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        items = data if isinstance(data, list | tuple) else [data]
        return [single(d, initial) for d in items if d] or [single(None, initial)]


class UploadForm(forms.Form):
    source = forms.ChoiceField(label=_("Exported from"), choices=Source.choices)
    mode = forms.ChoiceField(
        label=_("What to do with it"),
        choices=ImportRun.Mode.choices,
        widget=forms.RadioSelect,
        initial=ImportRun.Mode.COMMIT,
    )
    as_of = forms.DateField(
        label=_("Balances as of"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("The date the export was run."),
    )
    files = MultipleFileField(
        label=_("CSV files"),
        help_text=_("Select every CSV in the export at once (units, owners, ledger, balances…)."),
    )

    def clean_files(self):
        files = self.cleaned_data["files"]
        for f in files:
            if f.size > MAX_UPLOAD:
                raise forms.ValidationError(_("%(f)s is larger than 10 MB.") % {"f": f.name})
        return files
