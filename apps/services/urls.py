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
    path("<int:pk>/receita/", views.service_recipe, name="recipe"),
    path(
        "<int:pk>/receita/produtos/",
        views.service_recipe_product_picker,
        name="recipe_product_picker",
    ),
    path(
        "<int:pk>/receita/adicionar/",
        views.service_recipe_item_add,
        name="recipe_item_add",
    ),
    path(
        "receita/<int:item_id>/atualizar/",
        views.service_recipe_item_update,
        name="recipe_item_update",
    ),
    path(
        "receita/<int:item_id>/remover/",
        views.service_recipe_item_remove,
        name="recipe_item_remove",
    ),
]
