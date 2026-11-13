from django.urls import path

from . import views

app_name = "rules"

urlpatterns = [
    path("", views.rule_list, name="index"),
    path("override/", views.rule_override, name="override"),
    path("law-changes/", views.law_changes, name="law_changes"),
    path("law-changes/<int:pk>/reviewed/", views.law_changes_reviewed, name="law_changes_reviewed"),
]
