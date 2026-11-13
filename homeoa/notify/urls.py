from django.urls import path

from . import views

app_name = "notify"

# Mounted at "" ahead of the registry so /units/<number>/comms/ resolves here.
urlpatterns = [
    path("notifications/", views.inbox_list, name="inbox"),
    path("notifications/read-all/", views.inbox_read_all, name="inbox_read_all"),
    path("notifications/<int:pk>/read/", views.inbox_read, name="inbox_read"),
    path("units/<str:number>/comms/", views.unit_comms, name="unit_comms"),
    path(
        "settings/notifications/deliveries/<int:pk>/handled/",
        views.delivery_handled,
        name="delivery_handled",
    ),
]
