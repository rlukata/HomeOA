from django.urls import path

from . import views

app_name = "providers"

urlpatterns = [
    path("<str:token>/", views.download, name="download"),
]
