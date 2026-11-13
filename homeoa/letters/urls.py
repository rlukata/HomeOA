from django.urls import path

from . import views

app_name = "letters"

urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.new, name="new"),
    path("letterhead/", views.letterhead_edit, name="letterhead"),
    path("preview/", views.preview, name="preview"),
    path("<int:pk>/", views.edit, name="edit"),
    path("<int:pk>/preview.pdf", views.preview_pdf, name="preview_pdf"),
    path("<int:pk>/generate/", views.generate, name="generate"),
    path("<int:pk>/translations/", views.translation_upload, name="translation_upload"),
    path("translation/<int:pk>.pdf", views.translation_file, name="translation_file"),
    path("letter/<int:pk>.pdf", views.download, name="download"),
    path("batches/", views.batches, name="batches"),
    path("batches/<int:pk>/", views.batch_detail, name="batch"),
    path("batches/<int:pk>/send/", views.batch_send, name="batch_send"),
    path("batches/<int:pk>/mailed/", views.batch_mailed, name="batch_mailed"),
    path("batches/<int:pk>/batch.<str:which>", views.batch_file, name="batch_file"),
]
