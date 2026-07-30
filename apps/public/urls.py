from django.urls import path

from . import views

app_name = "public"

urlpatterns = [
    path("", views.salon_home, name="home"),
    path("agendar/", views.booking_service, name="booking_service"),
    path("agendar/profissional/", views.booking_employee, name="booking_employee"),
    path("agendar/horario/", views.booking_schedule, name="booking_schedule"),
    path(
        "agendar/horario/slots/",
        views.booking_schedule_slots,
        name="booking_schedule_slots",
    ),
    path("agendar/identificar/", views.booking_identify, name="booking_identify"),
    path("agendar/confirmar/", views.booking_confirm, name="booking_confirm"),
    path(
        "agendar/sucesso/<int:appointment_id>/",
        views.booking_success,
        name="booking_success",
    ),
    path("meus-agendamentos/", views.my_appointments, name="my_appointments"),
    path(
        "meus-agendamentos/<int:appointment_id>/cancelar/confirmar/",
        views.cancel_appointment_confirm,
        name="cancel_appointment_confirm",
    ),
    path(
        "meus-agendamentos/<int:appointment_id>/cancelar/",
        views.cancel_appointment_view,
        name="cancel_appointment",
    ),
    path(
        "meus-agendamentos/excluir-dados/confirmar/",
        views.delete_my_data_confirm,
        name="delete_my_data_confirm",
    ),
    path(
        "meus-agendamentos/excluir-dados/",
        views.delete_my_data_view,
        name="delete_my_data",
    ),
]
