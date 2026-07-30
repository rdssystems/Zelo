from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("", views.cash_list, name="list"),
    path("despesa/nova/", views.expense_create, name="expense_create"),
    path(
        "comissoes/<int:pk>/pagar/confirmar/",
        views.commission_pay_confirm,
        name="commission_pay_confirm",
    ),
    path("comissoes/<int:pk>/pagar/", views.commission_pay, name="commission_pay"),
    path(
        "comissoes/<int:employee_pk>/pagar-tudo/confirmar/",
        views.commission_pay_all_confirm,
        name="commission_pay_all_confirm",
    ),
    path(
        "comissoes/<int:employee_pk>/pagar-tudo/",
        views.commission_pay_all,
        name="commission_pay_all",
    ),
    path("comandas/cliente/<int:client_id>/produtos/", views.product_picker, name="product_picker"),
    path("comandas/produtos/<int:item_id>/atualizar/", views.comanda_item_update, name="comanda_item_update"),
    path("comandas/produtos/<int:item_id>/remover/", views.comanda_item_remove, name="comanda_item_remove"),
    path(
        "comandas/cliente/<int:client_id>/produtos/adicionar/",
        views.comanda_item_add,
        name="comanda_item_add",
    ),
    path("comandas/finalizar-grupo/", views.comanda_finalize_group, name="comanda_finalize_group"),
    path(
        "comandas/<int:pk>/remover/confirmar/",
        views.comanda_service_remove_confirm,
        name="comanda_service_remove_confirm",
    ),
    path("comandas/<int:pk>/remover/", views.comanda_service_remove, name="comanda_service_remove"),
    path(
        "comandas/cliente/<int:client_id>/servico/",
        views.walk_in_service_picker,
        name="walk_in_service_picker",
    ),
    path(
        "comandas/cliente/<int:client_id>/servico/<int:service_id>/profissionais/",
        views.walk_in_employee_list,
        name="walk_in_employee_list",
    ),
    path(
        "comandas/cliente/<int:client_id>/servico/adicionar/",
        views.walk_in_service_add,
        name="walk_in_service_add",
    ),
    path("vendas/nova/", views.sale_picker, name="sale_picker"),
    path("vendas/confirmar/", views.sale_create, name="sale_create"),
]
