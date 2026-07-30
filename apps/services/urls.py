from django.urls import path

from . import views

app_name = "services"

urlpatterns = [
    path("", views.service_list, name="list"),
    path("novo/", views.service_create, name="create"),
    path("<int:pk>/editar/", views.service_update, name="update"),
    path(
        "<int:pk>/toggle/confirmar/",
        views.service_toggle_confirm,
        name="toggle_confirm",
    ),
    path("<int:pk>/toggle/", views.service_toggle, name="toggle"),
    path(
        "<int:pk>/excluir/confirmar/",
        views.service_delete_confirm,
        name="delete_confirm",
    ),
    path("<int:pk>/excluir/", views.service_delete, name="delete"),
]
