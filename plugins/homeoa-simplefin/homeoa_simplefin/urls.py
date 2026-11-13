from django.urls import path

from . import views

app_name = "homeoa_simplefin"

# Mounted at /plugins/simplefin/ by homeoa.urls (PluginManifest.urls).
urlpatterns = [
    path("", views.index, name="index"),
    path("connect/", views.connect, name="connect"),
    path("link/", views.link_accounts, name="link"),
]
