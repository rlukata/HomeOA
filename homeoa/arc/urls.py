"""Mounted at "" in homeoa/urls.py: board pages under /requests/architectural/, the
resident pages under /me/arc/."""

from django.urls import path

from . import portal_views, views

app_name = "arc"

B = "requests/architectural/"
R = "me/arc/"

urlpatterns = [
    path(B, views.index, name="index"),
    path(B + "new/", views.board_new, name="new"),
    path(B + "forms/", views.forms_index, name="forms"),
    path(B + "forms/new/", views.form_edit, name="form_new"),
    path(B + "forms/<int:pk>/", views.form_edit, name="form_edit"),
    path(B + "reviewers/", views.reviewers_set, name="reviewers"),
    path(B + "<int:pk>/", views.detail, name="detail"),
    path(B + "<int:pk>/act/", views.act, name="act"),
    path(B + "<int:pk>/files/<int:att>/", views.file, name="file"),
    path(R, portal_views.mine, name="mine"),
    path(R + "new/", portal_views.new, name="mine_new"),
    path(R + "<int:pk>/", portal_views.detail, name="mine_detail"),
    path(R + "<int:pk>/act/", portal_views.act, name="mine_act"),
    path(R + "<int:pk>/letter/", portal_views.letter, name="mine_letter"),
    path(R + "<int:pk>/files/<int:att>/", portal_views.file, name="mine_file"),
]
