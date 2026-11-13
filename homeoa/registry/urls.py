from django.urls import path

from . import records_views, views

app_name = "registry"

urlpatterns = [
    path("", views.unit_list, name="index"),
    path("add/", views.unit_add, name="unit_add"),
    path("interests/", views.interests, name="interests"),
    path("records/", views.records, name="records"),
    path("records/full/", views.records_full, name="records_full"),
    path("records/requests/", records_views.board, name="records_requests"),
    path("records/requests/add/", records_views.add, name="records_request_add"),
    path("records/requests/<int:pk>/done/", records_views.done, name="records_request_done"),
    path("people/", views.people_list, name="people"),
    path("people/add/", views.person_add, name="person_add"),
    path("people/<int:pk>/", views.person_detail, name="person"),
    path("people/<int:pk>/edit/", views.person_edit, name="person_edit"),
    path("people/<int:pk>/notices/", views.person_notices, name="person_notices"),
    path(
        "people/<int:pk>/notices/<int:pref_pk>/evidence/",
        views.person_consent_evidence,
        name="person_consent_evidence",
    ),
    path("<str:number>/", views.unit_detail, name="unit"),
    path("<str:number>/edit/", views.unit_edit, name="unit_edit"),
    path("<str:number>/transfer/", views.unit_transfer, name="unit_transfer"),
    path("<str:number>/occupants/add/", views.occupant_add, name="occupant_add"),
    path("<str:number>/occupants/<int:pk>/lease/", views.lease, name="lease"),
    path("<str:number>/occupants/<int:pk>/end/", views.occupant_end, name="occupant_end"),
]
