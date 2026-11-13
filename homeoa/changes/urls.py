from django.urls import path

from . import views

app_name = "changes"

urlpatterns = [
    path("", views.index, name="index"),
    path("limits/", views.limits, name="limits"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/approve/", views.approve, name="approve"),
    path("<int:pk>/reject/", views.reject, name="reject"),
    path("<int:pk>/withdraw/", views.withdraw, name="withdraw"),
]
