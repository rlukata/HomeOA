from django import forms

from .models import Announcement, CalendarEvent


class AnnouncementForm(forms.ModelForm):
    class Meta:
        model = Announcement
        fields = ["title", "body", "audience", "publish_on", "expires_on", "pinned", "also_notify"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 6}),
            "publish_on": forms.DateInput(attrs={"type": "date"}),
            "expires_on": forms.DateInput(attrs={"type": "date"}),
        }


class CalendarEventForm(forms.ModelForm):
    class Meta:
        model = CalendarEvent
        fields = [
            "title",
            "start_date",
            "start_time",
            "end_date",
            "end_time",
            "location",
            "audience",
            "description",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }
