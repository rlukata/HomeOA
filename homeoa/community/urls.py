from django.urls import path

from . import poll_views, views

app_name = "community"

urlpatterns = [
    path("", views.announcements, name="index"),
    path("announcements/", views.announcements, name="announcements"),
    path("announcements/new/", views.announcement_edit, name="announcement_new"),
    path("announcements/<int:pk>/", views.announcement_edit, name="announcement_edit"),
    path("announcements/<int:pk>/delete/", views.announcement_delete, name="announcement_delete"),
    path("calendar/", views.calendar, name="calendar"),
    path("calendar/new/", views.event_edit, name="event_new"),
    path("calendar/<int:pk>/", views.event_edit, name="event_edit"),
    path("calendar/<int:pk>/delete/", views.event_delete, name="event_delete"),
    path("me/", views.resident, name="resident"),
    path("me/polls/<int:pk>/", poll_views.resident_poll, name="resident_poll"),
    path("polls/", poll_views.index, name="polls"),
    path("polls/<int:pk>/", poll_views.detail, name="poll"),
    path("polls/<int:pk>/close/", poll_views.close, name="poll_close"),
    path("feed/", views.feed_settings, name="feed"),
    path("feed/reset/", views.feed_reset, name="feed_reset"),
    path("feed/revoke/", views.feed_revoke, name="feed_revoke"),
    path("ical/<str:token>.ics", views.ical, name="ical"),
]
