from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.product_list, name="list"),
    path("novo/", views.product_create, name="create"),
    path("categorias/", views.category_list, name="category_list"),
    path("categorias/nova/", views.category_create, name="category_create"),
    path("categorias/<int:pk>/editar/", views.category_update, name="category_update"),
    path(
        "categorias/<int:pk>/excluir/confirmar/",
        views.category_delete_confirm,
        name="category_delete_confirm",
    ),
    path("categorias/<int:pk>/excluir/", views.category_delete, name="category_delete"),
    path("fornecedores/", views.supplier_list, name="supplier_list"),
    path("fornecedores/novo/", views.supplier_create, name="supplier_create"),
    path("fornecedores/<int:pk>/editar/", views.supplier_update, name="supplier_update"),
    path("fornecedores/<int:pk>/toggle/", views.supplier_toggle, name="supplier_toggle"),
    path("inventario/", views.inventory_count_list, name="count_list"),
    path("inventario/nova/", views.inventory_count_start, name="count_start"),
    path("inventario/<int:pk>/", views.inventory_count_detail, name="count_detail"),
    path(
        "inventario/<int:pk>/itens/<int:item_pk>/",
        views.inventory_count_item_update,
        name="count_item_update",
    ),
    path(
        "inventario/<int:pk>/fechar/confirmar/",
        views.inventory_count_close_confirm,
        name="count_close_confirm",
    ),
    path("inventario/<int:pk>/fechar/", views.inventory_count_close, name="count_close"),
    path("<int:pk>/editar/", views.product_update, name="update"),
    path(
        "<int:pk>/toggle/confirmar/",
        views.product_toggle_confirm,
        name="toggle_confirm",
    ),
    path("<int:pk>/toggle/", views.product_toggle, name="toggle"),
    path(
        "<int:pk>/excluir/confirmar/",
        views.product_delete_confirm,
        name="delete_confirm",
    ),
    path("<int:pk>/excluir/", views.product_delete, name="delete"),
    path("<int:pk>/movimentar/", views.product_movement, name="movement"),
    path("<int:pk>/lotes/", views.product_batches, name="batches"),
]
