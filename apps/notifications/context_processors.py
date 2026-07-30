from .services import unread_count_for_user


def unread_announcements(request):
    """Injeta `unread_announcements_count` em todo template — só calcula pra
    tenant_admin (sininho é exclusivo dele, decisão do usuário)."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or user.role != "tenant_admin":
        return {}
    return {"unread_announcements_count": unread_count_for_user(user)}
