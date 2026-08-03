from .services import unread_count_for_user, unread_tenant_notification_count


def unread_announcements(request):
    """Injeta as contagens do sininho em todo template — só calcula pra
    tenant_admin (sininho é exclusivo dele, decisão do usuário). O sininho
    mistura dois tipos (decisão do usuário em 2026-07-31): `Announcement`
    (aviso da plataforma pra todos os tenants) e `TenantNotification`
    (alerta operacional deste tenant, ex.: cliente cancelou agendamento) —
    `unread_bell_count` é a soma dos dois pro badge."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or user.role != "tenant_admin":
        return {}
    announcements_count = unread_count_for_user(user)
    tenant = getattr(request, "tenant", None)
    agenda_count = unread_tenant_notification_count(tenant) if tenant else 0
    return {
        "unread_announcements_count": announcements_count,
        "unread_agenda_notifications_count": agenda_count,
        "unread_bell_count": announcements_count + agenda_count,
    }
