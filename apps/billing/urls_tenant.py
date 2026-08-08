from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("", views.my_plan, name="my_plan"),
    path("assinar/<int:plan_id>/", views.select_plan_view, name="select_plan"),
    path("checkout/", views.checkout_view, name="checkout"),
    path("checkout/documento/", views.submit_document_view, name="submit_document"),
    path("checkout/status/", views.checkout_status, name="checkout_status"),
    path(
        "cancelar/confirmar/",
        views.cancel_subscription_confirm,
        name="cancel_subscription_confirm",
    ),
    path("cancelar/", views.cancel_subscription_view, name="cancel_subscription"),
]
