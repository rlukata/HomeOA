from django.urls import path

from . import upkeep_views, views

app_name = "maintenance"

# Mounted at "" in homeoa/urls.py: board pages under /requests/maintenance/, resident pages
# under /me/maintenance/.
urlpatterns = [
    path("requests/maintenance/", views.index, name="index"),
    path("requests/maintenance/add/", views.add, name="add"),
    path("requests/maintenance/<int:pk>/", views.detail, name="detail"),
    path("requests/maintenance/<int:pk>/status/", views.request_status, name="request_status"),
    path("requests/maintenance/<int:pk>/photos/", views.add_photos, name="add_photos"),
    path("requests/maintenance/<int:pk>/photos/<int:photo_pk>/", views.photo, name="photo"),
    path(
        "requests/maintenance/<int:pk>/work-order/",
        views.work_order_add,
        name="work_order_add",
    ),
    path("requests/work-orders/add/", views.work_order_add, name="work_order_new"),
    path("requests/work-orders/<int:pk>/", views.work_order, name="work_order"),
    path("requests/work-orders/<int:pk>/edit/", views.work_order_edit, name="work_order_edit"),
    path(
        "requests/work-orders/<int:pk>/status/",
        views.work_order_status,
        name="work_order_status",
    ),
    path("requests/work-orders/<int:pk>/bills/", views.link_bill, name="link_bill"),
    path(
        "requests/work-orders/<int:pk>/bills/<int:bill_pk>/unlink/",
        views.unlink_bill,
        name="unlink_bill",
    ),
    path("requests/work-orders/<int:pk>/print/", views.work_order_print, name="work_order_print"),
    path("requests/preventive/", upkeep_views.tasks, name="tasks"),
    path("requests/preventive/add/", upkeep_views.task_edit, name="task_add"),
    path("requests/preventive/<int:pk>/", upkeep_views.task_edit, name="task_edit"),
    path("requests/components/", upkeep_views.components, name="components"),
    path("requests/components/add/", upkeep_views.component_edit, name="component_add"),
    path("requests/components/<int:pk>/", upkeep_views.component_edit, name="component_edit"),
    path(
        "requests/components/<int:pk>/photos/",
        upkeep_views.component_photos,
        name="component_photos",
    ),
    path(
        "requests/components/<int:pk>/photos/<int:photo_pk>/",
        upkeep_views.component_photo,
        name="component_photo",
    ),
    path("me/maintenance/", views.my_requests, name="my_requests"),
    path("me/maintenance/new/", views.my_request_add, name="my_add"),
    path("me/maintenance/<int:pk>/", views.my_request_detail, name="my_detail"),
    path("me/maintenance/<int:pk>/photos/<int:photo_pk>/", views.my_photo, name="my_photo"),
]
