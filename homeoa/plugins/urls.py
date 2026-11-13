from django.urls import path

from . import views

app_name = "plugins"

urlpatterns = [
    path("settings/plugins/", views.plugin_list, name="index"),
    path("settings/plugins/<str:plugin_id>/", views.plugin_detail, name="detail"),
    path("hooks/<str:plugin_id>/<int:association_id>/", views.webhook, name="webhook"),
]
