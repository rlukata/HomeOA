from django.urls import path

from . import views

app_name = "setup"

# Mounted at /settings/setup/.
urlpatterns = [
    path("", views.index, name="index"),
    path("<slug:key>/", views.section, name="section"),
]
