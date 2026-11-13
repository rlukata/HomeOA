from django.urls import path

from . import views

app_name = "ap"

urlpatterns = [
    path("", views.index, name="index"),
    path("add/", views.add, name="add"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/submit/", views.submit, name="submit"),
    path("<int:pk>/void/", views.void, name="void"),
    path("<int:pk>/paid/", views.mark_paid, name="mark_paid"),
    path("<int:pk>/attach/", views.attach, name="attach"),
    path("<int:pk>/approve/", views.approve, name="approve"),
    path("<int:pk>/reject/", views.reject, name="reject"),
    path("approval-rules/", views.rules, name="rules"),
    path("approval-rules/add/", views.rule_add, name="rule_add"),
    path("approval-rules/<int:pk>/", views.rule_edit, name="rule_edit"),
    path("<int:pk>/files/<int:att>/", views.attachment, name="attachment"),
]
