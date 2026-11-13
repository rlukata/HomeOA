"""Catch-all hub/sub-tab placeholders for hubs no app has built yet. Mounted last."""

from django.urls import path

from . import views

app_name = "ui"

urlpatterns = [
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="service_worker"),
    path("offline/", views.offline, name="offline"),
    path("settings/", views.settings_page, name="settings"),
    path("<slug:hub_id>/", views.hub, name="hub"),
    path("<slug:hub_id>/<slug:tab_slug>/", views.subtab, name="subtab"),
]
