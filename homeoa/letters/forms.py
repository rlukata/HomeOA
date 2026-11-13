from django import forms
from django.utils.translation import gettext_lazy as _

from homeoa.registry.models import Unit

from . import render
from .models import LetterTemplate


class TemplateForm(forms.Form):
    name = forms.CharField(label=_("Name"), max_length=120)
    kind = forms.ChoiceField(label=_("Kind"), choices=LetterTemplate.Kind.choices)
    subject = forms.CharField(label=_("Subject"), max_length=200, required=False)
    body = forms.CharField(
        label=_("Letter text"),
        widget=forms.Textarea(attrs={"rows": 16}),
        help_text=_(
            "Use merge fields like {{ unit.number }}. A blank line starts a new paragraph."
        ),
    )
    draft_wording = forms.BooleanField(
        label=_("Draft wording, not legal advice"),
        required=False,
        help_text=_(
            "While checked, the letter footer says so. Clear it once a lawyer has reviewed it."
        ),
    )
    note = forms.CharField(label=_("Change note"), max_length=200, required=False)

    def clean(self):
        data = super().clean()
        result = render.check(data.get("subject", ""), data.get("body", ""))
        if not result.ok:
            for error in result.errors:
                self.add_error("body", error)
        return data


class GenerateForm(forms.Form):
    unit = forms.ModelChoiceField(label=_("Unit"), queryset=Unit.objects.none())
    as_of = forms.DateField(label=_("Letter date"), widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit"].queryset = Unit.objects.order_by("number")


class LetterheadForm(forms.Form):
    address = forms.CharField(
        label=_("Mailing address"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text=_("Up to four lines, printed under the association name."),
    )
    phone = forms.CharField(label=_("Phone"), max_length=40, required=False)
    email = forms.EmailField(label=_("Email"), required=False)


class BatchForm(forms.Form):
    name = forms.CharField(label=_("Batch name"), max_length=120, required=False)
    letters = forms.ModelMultipleChoiceField(
        label=_("Letters"),
        queryset=None,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Generated letters that are not in a batch yet."),
    )
    certified = forms.BooleanField(
        label=_("Certified mail"),
        required=False,
        help_text=_(
            "A mail service plugin sends these certified. For printed batches the "
            "manifest marks them, and you buy certified mail at the post office."
        ),
    )

    def __init__(self, *args, letters=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["letters"].queryset = letters
        self.fields["letters"].label_from_instance = lambda letter: (
            f"{letter.unit.number} · {letter.subject or letter.template_version.template}"
            f" · {letter.as_of.isoformat()}"
        )


class TranslationForm(forms.Form):
    """Board upload of an approved translation. Never machine-translated.

    It shares the edit page with ``TemplateForm`` (both have ``note``), so its ids get a
    ``tr_`` prefix; field names stay unprefixed so posts and tests are unchanged.
    """

    language = forms.ChoiceField(label=_("Language"))
    pdf = forms.FileField(
        label=_("Translation (PDF)"),
        required=False,
        help_text=_("Printed after the English letter."),
    )
    text = forms.CharField(
        label=_("Or the translated text"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
    )
    note = forms.CharField(label=_("Note (who translated it)"), max_length=200, required=False)
    approved = forms.BooleanField(
        label=_("The board approves this translation"),
        help_text=_("HomeOA never translates notices itself; it sends what you upload."),
    )

    def __init__(self, *args, **kwargs):
        from homeoa.registry.models import ENGLISH, PREFERRED_LANGUAGES

        kwargs.setdefault("auto_id", "tr_%s")
        super().__init__(*args, **kwargs)
        self.fields["language"].choices = [c for c in PREFERRED_LANGUAGES if c[0] != ENGLISH]

    def clean(self):
        data = super().clean()
        if not data.get("pdf") and not (data.get("text") or "").strip():
            raise forms.ValidationError(_("Upload a PDF or paste the translated text."))
        return data
