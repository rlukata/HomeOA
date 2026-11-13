from django.urls import path

from . import views

app_name = "budget"

urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.new, name="new"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/lines/", views.lines, name="lines"),
    path("<int:pk>/adopt/", views.adopt, name="adopt"),
    path("<int:pk>/actual/", views.actual, name="actual"),
    path("<int:pk>/dues/", views.dues, name="dues"),
]
