from django.urls import path

from . import views

app_name = "homeoa_stripe"

# Mounted at /plugins/stripe/ by homeoa.urls (PluginManifest.urls).
urlpatterns = [
    path("returns/<int:pk>/ack/", views.ack_return, name="ack_return"),
    path("bank/", views.bank_link, name="bank_link"),
    path("bank/collect/", views.bank_collect, name="bank_collect"),
    path("bank/confirm/", views.bank_confirm, name="bank_confirm"),
    path("bank/<int:pk>/verify/", views.bank_verify, name="bank_verify"),
    path("bank/<int:pk>/remove/", views.bank_remove, name="bank_remove"),
]
