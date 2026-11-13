from django.urls import path

from . import views
from .diff import views as diff_views

app_name = "imports"

urlpatterns = [
    path("units/import/", views.index, name="index"),
    path("units/import/<int:pk>/", views.run_detail, name="run"),
    path("units/import/<int:pk>/mapping/", views.mapping, name="mapping"),
    path("units/import/<int:pk>/commit/", views.commit, name="commit"),
    path("units/import/<int:pk>/purge/", views.purge, name="purge"),
    path("money/reports/parallel-run/", diff_views.index, name="parallel_run"),
    path("money/reports/cutover/", diff_views.cutover, name="cutover"),
    path("money/reports/parallel-run/run/<int:pk>/", diff_views.run, name="parallel_run_run"),
    path("money/reports/parallel-run/<int:pk>/", diff_views.detail, name="parallel_run_detail"),
    path("money/reports/parallel-run/line/<int:pk>/", diff_views.line, name="parallel_run_line"),
]
