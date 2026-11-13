from django.urls import path

from . import views

app_name = "resale_portal"

urlpatterns = [
    path("", views.portal, name="index"),
    path("<int:pk>/certificate.pdf", views.portal_pdf, name="pdf"),
]
