from django.urls import path

from . import autopay_views, views

app_name = "payments"

urlpatterns = [
    path("ledger/", views.ledger_home, name="ledger"),
    path("payments/", views.index, name="index"),
    path("payments/new/", views.new, name="new"),
    path("payments/unit-summary/", views.unit_summary, name="unit_summary"),
    path("payments/<int:pk>/", views.detail, name="detail"),
    path("payments/<int:pk>/void/", views.void, name="void"),
    path("payments/<int:pk>/refund/", views.refund, name="refund"),
    path("payments/autopay/<int:pk>/ack/", autopay_views.autopay_ack, name="autopay_ack"),
    path("payments/autopay/<int:pk>/resend/", autopay_views.autopay_resend, name="autopay_resend"),
    path("payments/autopay/settings/", autopay_views.autopay_settings, name="autopay_settings"),
    path("adjustments/", views.adjustments, name="adjustments"),
]
