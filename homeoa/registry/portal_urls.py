from django.urls import path

from . import records_views

app_name = "records_portal"

urlpatterns = [
    path("me/records/", records_views.portal, name="index"),
]
