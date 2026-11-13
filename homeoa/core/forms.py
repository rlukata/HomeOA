from zoneinfo import available_timezones

from django import forms
from django.utils.dates import MONTHS
from django.utils.translation import gettext_lazy as _

from .models import Association


def initials(name: str) -> str:
    """The first letter of up to three words: "Cedar Row Condominium" gives "CRC"."""
    return "".join(word[0] for word in name.split()[:3]).upper()


class NewAssociationForm(forms.ModelForm):
    """The association's name, initials, time zone and fiscal year on a new install."""

    timezone = forms.ChoiceField(
        label=_("Time zone"),
        help_text=_('Due dates and the association\'s "today" follow this time zone.'),
    )
    fiscal_year_start_month = forms.TypedChoiceField(
        label=_("Fiscal year starts in"),
        coerce=int,
        choices=list(MONTHS.items()),
        initial=1,
        help_text=_("The month the budget year starts."),
    )

    class Meta:
        model = Association
        fields = ["name", "short_name", "timezone", "fiscal_year_start_month"]
        labels = {"name": _("Association name"), "short_name": _("Initials")}
        help_texts = {
            "name": _("As it should appear on notices and statements."),
            "short_name": _("Shown in the logo badge. Left blank, they are taken from the name."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["short_name"].required = False
        zones = sorted(available_timezones())
        self.fields["timezone"].choices = [("", _("Choose a time zone"))] + [(z, z) for z in zones]

    def clean_short_name(self):
        short = self.cleaned_data.get("short_name", "").strip()
        return short or initials(self.cleaned_data.get("name", ""))[:12]
