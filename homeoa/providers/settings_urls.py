from django.urls import path

from . import views

app_name = "provider_settings"

urlpatterns = [
    path("", views.choose, name="choose"),
]
