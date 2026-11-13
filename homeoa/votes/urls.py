from django.urls import path

from . import views

app_name = "votes"

urlpatterns = [
    path("", views.index, name="index"),
    path("add/", views.create, name="create"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/open/", views.open_view, name="open"),
    path("<int:pk>/close/", views.close_view, name="close"),
    path("<int:pk>/paper/", views.paper, name="paper"),
]
