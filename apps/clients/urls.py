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
    path(
        "aniversariantes/whatsapp/",
        views.birthday_whatsapp_campaign,
        name="birthday_whatsapp_campaign",
    ),
    path("novo/", views.client_create, name="create"),
    path("pacotes/", views.package_list, name="package_list"),
    path("pacotes/novo/", views.package_create, name="package_create"),
    path("pacotes/<int:pk>/editar/", views.package_update, name="package_update"),
    path(
        "pacotes/<int:pk>/ativar-desativar/confirmar/",
        views.package_toggle_confirm,
        name="package_toggle_confirm",
    ),
    path("pacotes/<int:pk>/ativar-desativar/", views.package_toggle, name="package_toggle"),
    path("<int:pk>/editar/", views.client_update, name="update"),
    path("<int:pk>/preferencias/", views.client_preferences_modal, name="preferences_modal"),
    path("<int:pk>/preferencias/editar/", views.client_preferences_edit, name="preferences_edit"),
    path("<int:pk>/preferencias/atualizar/", views.client_preferences_update, name="preferences_update"),
    path("<int:pk>/mensalista/", views.client_subscription, name="subscription"),
    path(
        "<int:pk>/mensalista/renovar/confirmar/",
        views.client_renew_subscription_confirm,
        name="renew_subscription_confirm",
    ),
    path("<int:pk>/mensalista/renovar/", views.client_renew_subscription, name="renew_subscription"),
    path(
        "<int:pk>/mensalista/pacote/confirmar/",
        views.package_assign_confirm,
        name="package_assign_confirm",
    ),
    path("<int:pk>/mensalista/pacote/", views.package_assign, name="package_assign"),
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
