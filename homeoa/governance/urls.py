from django.urls import path

from . import views

app_name = "governance"

urlpatterns = [
    path("", views.index, name="index"),
    path("terms/add/", views.term_add, name="term_add"),
    path("terms/<int:pk>/end/", views.term_end, name="term_end"),
    path("committees/add/", views.committee_add, name="committee_add"),
    path("committees/<int:pk>/members/add/", views.member_add, name="member_add"),
    path("resolutions/add/", views.resolution_add, name="resolution_add"),
    path("resolutions/<int:pk>/", views.resolution, name="resolution"),
    path("resolutions/<int:pk>/act/", views.resolution_act, name="resolution_act"),
    path("resolutions/<int:pk>/pdf/", views.resolution_pdf, name="resolution_pdf"),
    path("actions/add/", views.action_add, name="action_add"),
    path("actions/<int:pk>/status/", views.action_status, name="action_status"),
]
