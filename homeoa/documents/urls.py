from django.urls import path

from . import library_views as lib
from . import views, welcome_views

app_name = "documents"

# Mounted at "" ahead of the registry so /units/<number>/file/ resolves here; the library
# lives under /documents/ (ahead of the ui hub placeholders).
urlpatterns = [
    path("units/<str:number>/file/", views.unit_file, name="unit_file"),
    path("units/<str:number>/file/ledger-export/", views.ledger_export, name="ledger_export"),
    path(
        "unit-file/ledger-exports/<int:pk>/<str:fmt>/",
        views.ledger_export_file,
        name="ledger_export_file",
    ),
    path("documents/", lib.section_view("all"), name="index"),
    path("documents/upload/", lib.upload, name="upload"),
    path("documents/welcome/", welcome_views.board, name="welcome"),
    path("me/welcome/", welcome_views.portal, name="portal_welcome"),
    *(
        path(f"documents/{slug}/", lib.section_view(slug), name=slug)
        for slug in ("governing", "financial", "meetings")
    ),
    path("documents/<uuid:public_id>/", lib.detail, name="detail"),
    path("documents/<uuid:public_id>/versions/", lib.new_version, name="new_version"),
    path("documents/<uuid:public_id>/visibility/", lib.change_visibility, name="visibility"),
    path("documents/<uuid:public_id>/download/", lib.download, name="download"),
    path(
        "documents/<uuid:public_id>/v<int:number>/download/",
        lib.download,
        name="download_version",
    ),
]
