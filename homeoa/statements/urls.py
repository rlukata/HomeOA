from django.urls import path

from . import statement_views as sv
from . import views

app_name = "statements"

urlpatterns = [
    path("money/statements/", sv.index, name="index"),
    path("money/statements/generate/", sv.generate, name="generate"),
    path("money/statements/<int:pk>/", sv.run_detail, name="statement_run"),
    path("money/statements/<int:pk>/send/", sv.send_run, name="send_run"),
    path("money/statements/statement/<int:pk>/send/", sv.send_statement, name="send"),
    path("money/statements/<int:pk>/batch/<int:batch_pk>.pdf", sv.batch_pdf, name="batch_pdf"),
    path("money/statements/pdf/<int:pk>.pdf", sv.statement_pdf, name="pdf"),
    path("money/delinquency/", views.delinquency, name="delinquency"),
    path("money/delinquency/run/", views.run_now, name="run"),
    path("money/delinquency/<int:pk>/hold/", views.hold, name="hold"),
    path("money/delinquency/<int:pk>/skip/", views.skip, name="skip"),
    path(
        "money/delinquency/demand/<int:pk>/reviewed/",
        views.demand_reviewed,
        name="demand_reviewed",
    ),
    path(
        "money/delinquency/action/<int:pk>/email-copy/",
        views.send_email_copy,
        name="send_email_copy",
    ),
    path("settings/delinquency/", views.settings_page, name="settings"),
]
