from django.urls import path

from . import views

app_name = "esign"

# Mounted at "": board pages under /governance/signatures/, signer pages under /sign/.
urlpatterns = [
    path("governance/signatures/", views.index, name="index"),
    path("governance/signatures/new/", views.new, name="new"),
    path("governance/signatures/<int:pk>/", views.detail, name="detail"),
    path("governance/signatures/<int:pk>/send/", views.send, name="send"),
    path("governance/signatures/<int:pk>/void/", views.void, name="void"),
    path("governance/signatures/<int:pk>/reviewed/", views.reviewed, name="reviewed"),
    path("governance/signatures/<int:pk>/resend/<int:signer>/", views.resend, name="resend"),
    path("governance/signatures/<int:pk>/file/<str:which>/", views.file, name="file"),
    path("sign/<str:token>/", views.sign, name="sign"),
    path("sign/<str:token>/document/", views.sign_document, name="sign_document"),
]
