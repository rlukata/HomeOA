from django.urls import path

from homeoa.payments import autopay_views

from . import views

app_name = "portal"

urlpatterns = [
    path("", views.home, name="home"),
    path("pay/", views.pay, name="pay"),
    path("pay/confirm/", views.pay_confirm, name="pay_confirm"),
    path("pay/autopay/", autopay_views.autopay_view, name="autopay"),
    path("payments/", views.payments, name="payments"),
    path("statements/", views.statements, name="statements"),
    path("statements/<int:pk>/pdf/", views.statement_pdf, name="statement_pdf"),
    path("profile/", views.profile_views.profile, name="profile"),
    path("profile/household/", views.profile_views.household_add, name="household_add"),
    path(
        "profile/household/<int:pk>/end/",
        views.profile_views.household_end,
        name="household_end",
    ),
    path("profile/consent/", views.profile_views.consent, name="consent"),
    path("profile/notifications/", views.profile_views.notifications, name="notifications"),
    path("requests/", views.requests_tab, name="requests"),
    path("docs/", views.docs, name="docs"),
    path("more/", views.more, name="more"),
]
