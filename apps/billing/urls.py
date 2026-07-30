from django.urls import path

from . import views

app_name = "plataforma"

urlpatterns = [
    path("", views.platform_dashboard, name="dashboard"),
    path("planos/", views.plan_list, name="plan_list"),
    path("planos/novo/", views.plan_create, name="plan_create"),
    path("planos/<int:pk>/editar/", views.plan_update, name="plan_update"),
    path("planos/<int:pk>/toggle/", views.plan_toggle, name="plan_toggle"),
    path("assinantes/", views.subscriber_list, name="subscriber_list"),
    path("assinantes/<uuid:tenant_id>/", views.subscriber_detail, name="subscriber_detail"),
    path(
        "assinantes/<uuid:tenant_id>/plano/",
        views.subscriber_change_plan,
        name="subscriber_change_plan",
    ),
    path(
        "assinantes/<uuid:tenant_id>/status/",
        views.subscriber_change_status,
        name="subscriber_change_status",
    ),
    path(
        "assinantes/<uuid:tenant_id>/periodo/",
        views.subscriber_change_period,
        name="subscriber_change_period",
    ),
    path(
        "assinantes/<uuid:tenant_id>/acesso/",
        views.subscriber_toggle_active,
        name="subscriber_toggle_active",
    ),
    path(
        "assinantes/<uuid:tenant_id>/excluir/confirmar/",
        views.subscriber_delete_confirm,
        name="subscriber_delete_confirm",
    ),
    path(
        "assinantes/<uuid:tenant_id>/excluir/",
        views.subscriber_delete,
        name="subscriber_delete",
    ),
]
