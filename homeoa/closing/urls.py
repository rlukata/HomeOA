from django.urls import path

from . import views

app_name = "closing"

# Mounted at /money/close/.
urlpatterns = [
    path("", views.index, name="index"),
    path("month/", views.close_month_view, name="close_month"),
    path("year/", views.close_year_view, name="close_year"),
    path("<int:pk>/reopen/", views.reopen_view, name="reopen"),
]
