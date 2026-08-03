from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django_ratelimit.decorators import ratelimit
from rest_framework import generics

from apps.accounts.decorators import tenant_admin_required
from apps.accounts.permissions import IsTenantAdminOrReadOnly, IsTenantMember

from .forms import BusinessHoursFormSet, DeleteAccountForm, SignUpForm, TenantSettingsForm
from .models import TenantBusinessHours, Weekday
from .serializers import TenantSettingsSerializer
from .services import delete_tenant_account, register_tenant, set_business_hours

# ---------------------------------------------------------------------------
# API REST (DRF) — /api/v1/tenant-settings/
# ---------------------------------------------------------------------------


class TenantSettingsView(generics.RetrieveUpdateAPIView):
    """Não é um recurso em lista — sempre "o tenant do usuário logado"."""

    serializer_class = TenantSettingsSerializer
    permission_classes = [IsTenantMember, IsTenantAdminOrReadOnly]

    def get_object(self):
        return self.request.user.tenant


# ---------------------------------------------------------------------------
# Cadastro self-service — /cadastrar/
# ---------------------------------------------------------------------------


@ratelimit(key="ip", rate="5/h", method="POST", block=False)
def signup_view(request):
    error = None
    form = SignUpForm(request.POST) if request.method == "POST" else SignUpForm()
    if request.method == "POST":
        if getattr(request, "limited", False):
            error = "Muitas tentativas. Aguarde alguns minutos e tente novamente."
        elif form.is_valid():
            try:
                tenant, user = register_tenant(
                    name=form.cleaned_data["name"],
                    email=form.cleaned_data["email"],
                    password=form.cleaned_data["password1"],
                    theme=form.cleaned_data["theme"],
                )
            except ValidationError as exc:
                for field, errors in getattr(
                    exc, "message_dict", {"__all__": exc.messages}
                ).items():
                    for msg in errors:
                        form.add_error(field if field in form.fields else None, msg)
            else:
                # Cadastro self-service sempre autentica por senha própria
                # (nunca via Google) — especificar o backend evita o erro
                # "multiple authentication backends" que o allauth introduziu
                # (ver AUTHENTICATION_BACKENDS em config/settings.py).
                auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                messages.success(
                    request,
                    f"Salão criado! Sua página pública é /{tenant.slug}/ — "
                    f"você já pode configurar tudo por aqui.",
                )
                return redirect("painel_home")
    return render(request, "painel/signup.html", {"form": form, "error": error})


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/configuracoes/
# ---------------------------------------------------------------------------


def _business_hours_initial(tenant):
    hours_by_weekday = {
        h.weekday: h
        for h in TenantBusinessHours.objects.for_tenant(tenant).order_by("weekday")
    }
    return [
        {
            "weekday": weekday,
            "is_closed": hours_by_weekday[weekday].is_closed if weekday in hours_by_weekday else True,
            "start_time": hours_by_weekday[weekday].start_time if weekday in hours_by_weekday else None,
            "end_time": hours_by_weekday[weekday].end_time if weekday in hours_by_weekday else None,
        }
        for weekday, _ in Weekday.choices
    ]


def _hours_rows(hours_formset):
    """Zip cada form do formset com o label do dia — a ordem do formset é
    sempre 0..6 (`_business_hours_initial`/POST round-trip preservam isso)."""
    return list(zip(hours_formset.forms, [label for _, label in Weekday.choices]))


@tenant_admin_required
def settings_view(request):
    tenant = request.tenant
    if request.method == "POST":
        form = TenantSettingsForm(request.POST, request.FILES, instance=tenant)
        hours_formset = BusinessHoursFormSet(request.POST)
        if form.is_valid() and hours_formset.is_valid():
            form.save()
            try:
                set_business_hours(
                    tenant,
                    [
                        {
                            "weekday": f.cleaned_data["weekday"],
                            "is_closed": f.cleaned_data["is_closed"],
                            "start_time": f.cleaned_data.get("start_time"),
                            "end_time": f.cleaned_data.get("end_time"),
                        }
                        for f in hours_formset
                    ],
                )
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                messages.success(request, "Configurações atualizadas.")
                return redirect("tenants:settings")
    else:
        form = TenantSettingsForm(instance=tenant)
        hours_formset = BusinessHoursFormSet(initial=_business_hours_initial(tenant))
    return render(
        request,
        "painel/tenants/settings.html",
        {
            "form": form,
            "tenant": tenant,
            "active_nav": "settings",
            "hours_formset": hours_formset,
            "hours_rows": _hours_rows(hours_formset),
        },
    )


@tenant_admin_required
def delete_account_confirm(request):
    return render(
        request,
        "painel/tenants/_confirm_delete_account.html",
        {"form": DeleteAccountForm(), "tenant": request.tenant},
    )


@tenant_admin_required
def delete_account_view(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    form = DeleteAccountForm(request.POST)
    if not form.is_valid():
        response = render(
            request,
            "painel/tenants/_confirm_delete_account.html",
            {"form": form, "tenant": request.tenant},
        )
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    delete_tenant_account(request.tenant)
    auth_logout(request)
    response = HttpResponse()
    response.headers["HX-Redirect"] = reverse("account_deleted")
    return response
