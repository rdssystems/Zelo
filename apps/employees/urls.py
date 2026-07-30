from django.urls import path

from . import views

app_name = "employees"

urlpatterns = [
    path("", views.employee_list, name="list"),
    path("novo/", views.employee_create, name="create"),
    path("<int:pk>/editar/", views.employee_update, name="update"),
    path(
        "<int:pk>/toggle/confirmar/",
        views.employee_toggle_confirm,
        name="toggle_confirm",
    ),
    path("<int:pk>/toggle/", views.employee_toggle, name="toggle"),
    path(
        "<int:pk>/excluir/confirmar/",
        views.employee_delete_confirm,
        name="delete_confirm",
    ),
    path("<int:pk>/excluir/", views.employee_delete, name="delete"),
    path("<int:pk>/jornada/", views.employee_working_hours, name="working_hours"),
    path("<int:pk>/servicos/", views.employee_services, name="services"),
]
