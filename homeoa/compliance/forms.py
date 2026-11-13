from django import forms

from .models import ManualItem


class ManualItemForm(forms.ModelForm):
    class Meta:
        model = ManualItem
        fields = ["title", "anchor", "recurrence", "notes"]
        widgets = {"anchor": forms.DateInput(attrs={"type": "date"})}
