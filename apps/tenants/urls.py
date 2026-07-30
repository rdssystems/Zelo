from django.urls import path

from . import views

app_name = "tenants"

urlpatterns = [
    path("", views.settings_view, name="settings"),
    path(
        "excluir-conta/confirmar/",
        views.delete_account_confirm,
        name="delete_account_confirm",
    ),
    path("excluir-conta/", views.delete_account_view, name="delete_account"),
]
