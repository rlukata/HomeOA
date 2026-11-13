from django.urls import path

from . import views

app_name = "vendors"

urlpatterns = [
    path("", views.vendor_list, name="index"),
    path("add/", views.vendor_add, name="add"),
    path("1099/", views.ten99_view, name="ten99"),
    path("certificates/<int:pk>/file/", views.document_file, name="document_file"),
    path("certificates/<int:pk>/request/", views.document_request, name="document_request"),
    path("contracts/<int:pk>/file/", views.contract_file, name="contract_file"),
    path("alerts/<int:pk>/done/", views.alert_resolve, name="alert_resolve"),
    path("<slug:slug>/", views.vendor_detail, name="detail"),
    path("<slug:slug>/certificates/add/", views.document_add, name="document_add"),
    path("<slug:slug>/contracts/add/", views.contract_edit, name="contract_add"),
    path("<slug:slug>/contracts/<int:pk>/", views.contract_edit, name="contract_edit"),
    path("<slug:slug>/edit/", views.vendor_edit, name="edit"),
    path("<slug:slug>/tin/", views.reveal_tin, name="reveal_tin"),
    path("<slug:slug>/w9/", views.w9_download, name="w9"),
    path("<slug:slug>/contacts/add/", views.contact_edit, name="contact_add"),
    path("<slug:slug>/contacts/<int:pk>/", views.contact_edit, name="contact_edit"),
    path("<slug:slug>/contacts/<int:pk>/delete/", views.contact_delete, name="contact_delete"),
]
