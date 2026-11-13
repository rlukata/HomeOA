from django.urls import path

from . import directory_views

app_name = "directory"

urlpatterns = [
    path("community/directory/", directory_views.board, name="board"),
    path("me/directory/", directory_views.portal, name="portal"),
]
