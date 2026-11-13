from django.urls import path

from . import views

app_name = "assist"

urlpatterns = [path("bill-from-invoice/", views.bill_from_invoice, name="bill_from_invoice")]
