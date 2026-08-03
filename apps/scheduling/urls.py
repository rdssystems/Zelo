from django.urls import path

from . import views

app_name = "scheduling"

urlpatterns = [
    path("", views.agenda_list, name="list"),
    path("atualizar/", views.agenda_items_poll, name="agenda_items_poll"),
    path("semana/", views.agenda_week, name="agenda_week"),
    path("semana/atualizar/", views.agenda_week_poll, name="agenda_week_poll"),
    path("<int:pk>/detalhe/", views.appointment_detail, name="detail"),
    path("novo/", views.appointment_new, name="new"),
    path("novo/horarios/", views.new_appointment_slots, name="new_slots"),
    path(
        "<int:pk>/confirmar/preparar/",
        views.appointment_confirm_prepare,
        name="confirm_prepare",
    ),
    path("<int:pk>/confirmar/", views.appointment_confirm, name="confirm"),
    path("<int:pk>/iniciar/", views.appointment_start, name="start"),
    path(
        "<int:pk>/cancelar/confirmar/",
        views.appointment_cancel_confirm,
        name="cancel_confirm",
    ),
    path("<int:pk>/cancelar/", views.appointment_cancel, name="cancel"),
    path(
        "<int:pk>/nao-compareceu/confirmar/",
        views.appointment_no_show_confirm,
        name="no_show_confirm",
    ),
    path("<int:pk>/nao-compareceu/", views.appointment_no_show, name="no_show"),
]
