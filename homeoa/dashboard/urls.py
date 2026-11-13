from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("todos/", views.todo_list, name="todos"),
    path("todos/<str:item_id>/act/", views.todo_act, name="todo_act"),
    path("money/", views.money_overview, name="money"),
]
