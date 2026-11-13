from django.urls import path

from . import views

app_name = "access"

urlpatterns = [
    path("", views.index, name="index"),
    path("assign/", views.assign, name="assign"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/end/", views.end, name="end"),
    path("custom/", views.add_role, name="add_role"),
    path("security/", views.security, name="security"),
    path("status/", views.association_status_page, name="status"),
    path("go-live/", views.go_live, name="go_live"),
]
