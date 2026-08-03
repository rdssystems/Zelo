from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.notification_list, name="list"),
    path("<int:pk>/marcar-lida/", views.mark_read, name="mark_read"),
    path(
        "agenda/<int:pk>/marcar-lida/",
        views.mark_tenant_notification_read,
        name="mark_tenant_notification_read",
    ),
    path("marcar-todas-lidas/", views.mark_all_read, name="mark_all_read"),
    path("toast/", views.agenda_toast_poll, name="agenda_toast_poll"),
]
