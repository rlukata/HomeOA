from django.urls import path

from . import latefee_views as lf
from . import views

app_name = "billing"

urlpatterns = [
    path("", views.index, name="index"),
    path("run/", views.run, name="run"),
    path("post/", views.post, name="post"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("schedules/new/", views.schedule_edit, name="schedule_new"),
    path("schedules/<int:pk>/", views.schedule_edit, name="schedule_edit"),
    # Late fees, interest, special assessments
    path("late-fees/", lf.late_fees, name="late_fees"),
    path("late-fees/policies/new/", lf.policy_edit, name="policy_new"),
    path("late-fees/policies/<int:pk>/", lf.policy_edit, name="policy_edit"),
    path("late-fees/<int:pk>/waive/", lf.waive, name="late_fee_waive"),
    path("special/", lf.specials, name="specials"),
    path("special/new/", lf.special_new, name="special_new"),
    path("special/<int:pk>/", lf.special_detail, name="special_detail"),
    path("special/<int:pk>/ratification/", lf.special_ratify, name="special_ratify"),
    path("special/<int:pk>/emergency/", lf.special_emergency, name="special_emergency"),
    path("special/installments/<int:pk>/post/", lf.installment_post, name="installment_post"),
]
