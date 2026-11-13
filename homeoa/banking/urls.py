from django.urls import path

from . import views

app_name = "banking"

# Mounted at /money/bank/.
urlpatterns = [
    path("", views.index, name="index"),
    path("import/", views.import_view, name="import"),
    path("sync/", views.sync, name="sync"),
    path("imports/<int:pk>/", views.batch_detail, name="batch"),
    path("accounts/new/", views.account_edit, name="account_new"),
    path("accounts/<int:pk>/", views.account_edit, name="account_edit"),
    path("accounts/<int:pk>/mapping/", views.mapping_edit, name="mapping"),
    path("txn/<int:pk>/", views.txn_detail, name="txn"),
    path("txn/<int:pk>/confirm/", views.confirm, name="confirm"),
    path("txn/<int:pk>/split/", views.split, name="split"),
    path("txn/<int:pk>/categorize/", views.categorize, name="categorize"),
    path("txn/<int:pk>/undo/", views.undo, name="undo"),
    path("rules/", views.rules, name="rules"),
    path("reserve/", views.reserve_withdrawals, name="reserve"),
    path("reconcile/", views.reconcile_index, name="reconcile"),
    path("reconcile/<int:pk>/", views.reconciliation, name="reconciliation"),
]
