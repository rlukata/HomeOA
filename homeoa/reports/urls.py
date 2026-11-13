from django.urls import path

from . import packet_views, views

app_name = "reports"

# Mounted at /money/reports/.
urlpatterns = [
    path("", views.index, name="index"),
    path("trial-balance/", views.trial_balance_view, name="trial_balance"),
    path("gl/", views.gl_view, name="gl"),
    path("ar-aging/", views.ar_aging_view, name="ar_aging"),
    path("income-statement/", views.income_statement_view, name="income_statement"),
    path("balance-sheet/", views.balance_sheet_view, name="balance_sheet"),
    path("cash-flow/", views.cash_flow_view, name="cash_flow"),
    path("ap-aging/", views.ap_aging_view, name="ap_aging"),
    path("cpa-package/", views.cpa_package_view, name="cpa_package"),
    path("form-1120-h/", views.tax_1120h_view, name="tax_1120h"),
    path("exports/<slug:slug>.csv", views.module_export_view, name="module_export"),
    path("board-packet/", packet_views.board_packet_view, name="board_packet"),
]
