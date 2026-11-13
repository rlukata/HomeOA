from django.urls import path

from . import portal_views

app_name = "votes_portal"

urlpatterns = [
    path("", portal_views.index, name="index"),
    path("<int:pk>/", portal_views.vote, name="vote"),
    path("<int:pk>/receipt/<int:ballot>/", portal_views.receipt, name="receipt"),
]
