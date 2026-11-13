from django.urls import path

from . import portal_views

app_name = "violations_portal"

urlpatterns = [
    path("<int:pk>/", portal_views.detail, name="detail"),
    path("<int:pk>/respond/", portal_views.respond, name="respond"),
    path("<int:pk>/photos/<int:doc>/", portal_views.photo, name="photo"),
    path("<int:pk>/letters/<int:step>/", portal_views.letter, name="letter"),
]
