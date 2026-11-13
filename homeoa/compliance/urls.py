from django.urls import path

from . import views

app_name = "compliance"

urlpatterns = [
    path("", views.index, name="index"),
    path("check/", views.check_now, name="check"),
    path("<int:pk>/", views.period, name="period"),
    path("<int:pk>/act/", views.act, name="act"),
    path("items/add/", views.item_edit, name="item_add"),
    path("items/<int:pk>/", views.item_edit, name="item"),
    path("items/<int:pk>/done/", views.item_done, name="item_done"),
    path("items/<int:pk>/remove/", views.item_remove, name="item_remove"),
    path("reminders/", views.reminder_settings, name="reminders"),
    path("evidence/<int:pk>/", views.evidence_file, name="evidence_file"),
]
