from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string

from apps.accounts.decorators import superadmin_required

from . import services as notif_ops
from .forms import AnnouncementForm
from .models import Announcement

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
    return render_to_string(
        "painel/notifications/_bell.html",
        {"unread_announcements_count": notif_ops.unread_count_for_user(request.user), "oob": True},
        request=request,
    )


@login_required
def notification_list(request):
    if request.user.role != "tenant_admin":
        return HttpResponse(status=403)
    announcements = notif_ops.unread_announcements_for_user(request.user)
    return render(request, "painel/notifications/_list.html", {"announcements": announcements})


@login_required
def mark_read(request, pk):
    if request.user.role != "tenant_admin":
        return HttpResponse(status=403)
    if request.method != "POST":
        return HttpResponse(status=405)
    announcement = get_object_or_404(Announcement, pk=pk)
    notif_ops.mark_read(announcement, request.user)
    list_html = render_to_string(
        "painel/notifications/_list.html",
        {"announcements": notif_ops.unread_announcements_for_user(request.user)},
        request=request,
    )
    return HttpResponse(list_html + _bell_html(request))


@login_required
def mark_all_read(request):
    if request.user.role != "tenant_admin":
        return HttpResponse(status=403)
    if request.method != "POST":
        return HttpResponse(status=405)
    notif_ops.mark_all_read(request.user)
    list_html = render_to_string(
        "painel/notifications/_list.html", {"announcements": []}, request=request
    )
    return HttpResponse(list_html + _bell_html(request))
