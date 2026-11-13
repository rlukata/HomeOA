from django.urls import path

from . import views

app_name = "enforcement"

urlpatterns = [
    path("", views.index, name="index"),
    path("add/", views.violation_add, name="add"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/status/", views.violation_status, name="status"),
    path("<int:pk>/notice/", views.notice_send, name="notice"),
    path("<int:pk>/cured/", views.cured, name="cured"),
    path("<int:pk>/photos/", views.photo_add, name="photo_add"),
    path("<int:pk>/hearing/", views.hearing_schedule, name="hearing"),
    path("hearings/<int:pk>/decision/", views.hearing_decision, name="decision"),
    path("hearings/<int:pk>/reschedule/", views.hearing_reschedule, name="reschedule"),
    path("hearings/<int:pk>/cancel/", views.hearing_cancel, name="hearing_cancel"),
    path("hearings/<int:pk>/void/", views.fine_void, name="fine_void"),
    path("categories/", views.categories, name="categories"),
    path("categories/<int:pk>/", views.category_edit, name="category_edit"),
    path("schedules/", views.schedules, name="schedules"),
    path("schedules/<int:pk>/", views.schedule_detail, name="schedule"),
]
