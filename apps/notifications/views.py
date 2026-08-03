from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string

from apps.accounts.decorators import superadmin_required, tenant_admin_required

from . import services as notif_ops
from .forms import AnnouncementForm
from .models import Announcement, TenantNotification

# ---------------------------------------------------------------------------
# Superadmin (CRUD) — /plataforma/avisos/
# ---------------------------------------------------------------------------


def _announcement_items_response(request):
    items = render_to_string(
        "plataforma/announcements/_items.html",
        {"announcements": Announcement.objects.all()},
        request=request,
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(items + modal_reset)


@superadmin_required
def announcement_list(request):
    return render(
        request,
        "plataforma/announcements/list.html",
        {"announcements": Announcement.objects.all(), "active_nav": "announcements"},
    )


@superadmin_required
def announcement_create(request):
    if request.method == "POST":
        form = AnnouncementForm(request.POST)
        if form.is_valid():
            notif_ops.create_announcement(created_by=request.user, **form.cleaned_data)
            return _announcement_items_response(request)
    else:
        form = AnnouncementForm()
    response = render(
        request, "plataforma/announcements/_form.html", {"form": form, "title": "Novo Aviso"}
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@superadmin_required
def announcement_update(request, pk):
    announcement = get_object_or_404(Announcement, pk=pk)
    if request.method == "POST":
        form = AnnouncementForm(request.POST)
        if form.is_valid():
            notif_ops.update_announcement(announcement, **form.cleaned_data)
            return _announcement_items_response(request)
    else:
        form = AnnouncementForm(
            initial={"title": announcement.title, "message": announcement.message}
        )
    response = render(
        request,
        "plataforma/announcements/_form.html",
        {"form": form, "title": "Editar Aviso", "announcement": announcement},
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@superadmin_required
def announcement_toggle(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    announcement = get_object_or_404(Announcement, pk=pk)
    notif_ops.set_announcement_active(announcement, not announcement.is_active)
    return _announcement_items_response(request)


# ---------------------------------------------------------------------------
# Tenant admin (sininho) — /painel/avisos/
# ---------------------------------------------------------------------------


def _bell_html(request):
    announcements_count = notif_ops.unread_count_for_user(request.user)
    agenda_count = notif_ops.unread_tenant_notification_count(request.tenant)
    return render_to_string(
        "painel/notifications/_bell.html",
        {"unread_bell_count": announcements_count + agenda_count, "oob": True},
        request=request,
    )


def _list_context(request):
    return {
        "announcements": notif_ops.unread_announcements_for_user(request.user),
        "agenda_notifications": notif_ops.unread_tenant_notifications(request.tenant),
    }


@login_required
def notification_list(request):
    if request.user.role != "tenant_admin":
        return HttpResponse(status=403)
    return render(request, "painel/notifications/_list.html", _list_context(request))


@login_required
def mark_read(request, pk):
    if request.user.role != "tenant_admin":
        return HttpResponse(status=403)
    if request.method != "POST":
        return HttpResponse(status=405)
    announcement = get_object_or_404(Announcement, pk=pk)
    notif_ops.mark_read(announcement, request.user)
    list_html = render_to_string("painel/notifications/_list.html", _list_context(request), request=request)
    return HttpResponse(list_html + _bell_html(request))


@login_required
def mark_tenant_notification_read(request, pk):
    if request.user.role != "tenant_admin":
        return HttpResponse(status=403)
    if request.method != "POST":
        return HttpResponse(status=405)
    notification = get_object_or_404(
        TenantNotification.objects.for_tenant(request.tenant), pk=pk
    )
    notif_ops.mark_tenant_notification_read(notification)
    list_html = render_to_string("painel/notifications/_list.html", _list_context(request), request=request)
    return HttpResponse(list_html + _bell_html(request))


@login_required
def mark_all_read(request):
    if request.user.role != "tenant_admin":
        return HttpResponse(status=403)
    if request.method != "POST":
        return HttpResponse(status=405)
    notif_ops.mark_all_read(request.user)
    notif_ops.mark_all_tenant_notifications_read(request.tenant)
    list_html = render_to_string("painel/notifications/_list.html", _list_context(request), request=request)
    return HttpResponse(list_html + _bell_html(request))


# ---------------------------------------------------------------------------
# Polling do "aviso no cantinho" — /painel/avisos/toast/
# ---------------------------------------------------------------------------


@tenant_admin_required
def agenda_toast_poll(request):
    """`hx-trigger="every Ns"` em `painel/base.html` — devolve só as
    notificações operacionais ainda não exibidas nesta sessão (watermark por
    `pk`, não por `is_read`: o admin pode ver o toast e ele continuar não
    lido no sininho até ele marcar de verdade)."""
    last_seen_id = request.session.get("agenda_toast_last_id", 0)
    new_notifications = list(notif_ops.new_tenant_notifications_since(request.tenant, last_seen_id))
    if new_notifications:
        request.session["agenda_toast_last_id"] = new_notifications[-1].pk
    html = render_to_string(
        "painel/notifications/_toast_poll.html",
        {"notifications": new_notifications},
        request=request,
    )
    return HttpResponse(html + _bell_html(request))
