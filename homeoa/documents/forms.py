from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Category, GoverningKind, Visibility


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, list | tuple):
            return [single(d, initial) for d in data]
        return [single(data, initial)]


class UploadForm(forms.Form):
    files = MultipleFileField(label=_("Files"))
    category = forms.ChoiceField(label=_("Category"), choices=Category.choices)
    visibility = forms.ChoiceField(
        label=_("Visible to"), choices=Visibility.choices, initial=Visibility.BOARD
    )
    title = forms.CharField(
        label=_("Title"),
        max_length=200,
        required=False,
        help_text=_("For a single file. Several files are titled by their file names."),
    )
    governing_kind = forms.ChoiceField(
        label=_("Governing document kind"),
        choices=[("", "—"), *GoverningKind.choices],
        required=False,
    )
    effective_date = forms.DateField(
        label=_("Effective date"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    note = forms.CharField(label=_("Note"), max_length=500, required=False)

    def clean(self):
        data = super().clean()
        if data.get("governing_kind") and data.get("category") != Category.GOVERNING:
            self.add_error("governing_kind", _("Only governing documents have a kind."))
        return data


class VersionForm(forms.Form):
    file = forms.FileField(label=_("File"))
    effective_date = forms.DateField(
        label=_("Effective date"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    note = forms.CharField(
        label=_("Note"),
        max_length=500,
        required=False,
        help_text=_("For example: amendment adopted by owner vote."),
    )


class VisibilityForm(forms.Form):
    visibility = forms.ChoiceField(label=_("Visible to"), choices=Visibility.choices)
    in_welcome_packet = forms.BooleanField(
        label=_("In the owners' welcome packet"),
        required=False,
        help_text=_("Shown on the Welcome page new owners get. Not for board-only documents."),
    )

    def clean(self):
        data = super().clean()
        if data.get("in_welcome_packet") and data.get("visibility") == Visibility.BOARD:
            self.add_error(
                "in_welcome_packet", _("Board-only documents can't go in the welcome packet.")
            )
        return data
