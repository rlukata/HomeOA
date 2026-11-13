from django.urls import path

from . import views

app_name = "meetings"

urlpatterns = [
    path("", views.index, name="index"),
    path("add/", views.add, name="add"),
    path("live/", views.live, name="live"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/emergency/", views.emergency, name="emergency"),
    path("<int:pk>/status/", views.status, name="status"),
    path("<int:pk>/agenda/add/", views.agenda_add, name="agenda_add"),
    path("<int:pk>/agenda/<int:item>/move/", views.agenda_move, name="agenda_move"),
    path("<int:pk>/agenda/<int:item>/remove/", views.agenda_remove, name="agenda_remove"),
    path("<int:pk>/notice/", views.send, name="send"),
    path("<int:pk>/annual/", views.annual, name="annual"),
    path("<int:pk>/annual/do/", views.annual_action, name="annual_action"),
    path("<int:pk>/minutes/", views.minutes, name="minutes"),
    path("<int:pk>/minutes/<str:action>/", views.minutes_action, name="minutes_action"),
    path("<int:pk>/attendance/", views.attendance, name="attendance"),
    path("<int:pk>/attendance/set/", views.attendance_set, name="attendance_set"),
    path("<int:pk>/proxies/add/", views.proxy_add, name="proxy_add"),
    path("<int:pk>/proxies/<int:proxy>/revoke/", views.proxy_revoke, name="proxy_revoke"),
    path("<int:pk>/quorum/", views.quorum_setting, name="quorum_setting"),
]
