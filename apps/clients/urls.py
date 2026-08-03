from django.urls import path

from . import views

app_name = "clients"

urlpatterns = [
    path("", views.client_list, name="list"),
    path(
        "mensalistas/whatsapp/",
        views.subscription_whatsapp_campaign,
        name="subscription_whatsapp_campaign",
    ),
    path("novo/", views.client_create, name="create"),
    path("<int:pk>/editar/", views.client_update, name="update"),
    path("<int:pk>/mensalista/", views.client_subscription, name="subscription"),
    path("<int:pk>/mensalista/renovar/", views.client_renew_subscription, name="renew_subscription"),
    path(
        "<int:pk>/credito/creditar/confirmar/",
        views.client_credit_add_confirm,
        name="credit_add_confirm",
    ),
    path("<int:pk>/credito/creditar/", views.client_credit_add, name="credit_add"),
    path(
        "<int:pk>/credito/remover/confirmar/",
        views.client_credit_remove_confirm,
        name="credit_remove_confirm",
    ),
    path("<int:pk>/credito/remover/", views.client_credit_remove, name="credit_remove"),
    path("<int:pk>/excluir/confirmar/", views.client_delete_confirm, name="delete_confirm"),
    path("<int:pk>/excluir/", views.client_delete, name="delete"),
]
