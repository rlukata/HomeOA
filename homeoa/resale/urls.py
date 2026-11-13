from django.urls import path

from . import views

app_name = "resale"

urlpatterns = [
    path("", views.index, name="index"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/issue/", views.issue, name="issue"),
    path("<int:pk>/cancel/", views.cancel, name="cancel"),
    path("<int:pk>/certificate.pdf", views.pdf, name="pdf"),
]
