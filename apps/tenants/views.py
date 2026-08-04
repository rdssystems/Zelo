from allauth.account.models import EmailAddress
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
from .models import TenantBusinessHours, TenantTheme, Weekday
from .serializers import TenantSettingsSerializer
from .services import (
    delete_tenant_account,
    register_tenant,
    set_business_hours,
    theme_from_host,
)

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
# Landing page — domínio raiz (`/`)
# ---------------------------------------------------------------------------


def landing_view(request):
    """Domínio raiz mostra a landing com os 2 caminhos (barbearia/salão).
    Quem cair na raiz de um subdomínio dedicado (`barbearia.`/`salao.`)
    vai direto pra tela de login/cadastro daquele tema — decisão do
    usuário em 2026-08-03."""
    if theme_from_host(request.get_host()):
        return redirect("login")
    return render(request, "landing.html")


# ---------------------------------------------------------------------------
# Cadastro self-service — /cadastrar/
# ---------------------------------------------------------------------------


@ratelimit(key="ip", rate="5/h", method="POST", block=False)
def signup_view(request):
    """`detected_theme` (subdomínio `barbearia.`/`salao.`, decisão do
    usuário em 2026-08-03): quando presente, o template esconde o rádio de
    escolha de tema e manda um campo oculto no lugar — mas quem decide o
    tema de verdade é o servidor (`detected_theme` tem prioridade sobre o
    que veio no POST), não o valor enviado pelo cliente. O `SignUpForm`
    não muda, o campo `theme` continua obrigatório."""
    error = None
    detected_theme = theme_from_host(request.get_host())
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
                    theme=detected_theme or form.cleaned_data["theme"],
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
                # Dispara e-mail de confirmação (allauth) — só pra este
                # caminho local. O cadastro via Google já chega com e-mail
                # verificado pelo próprio Google (ver adapters.py), não passa
                # por aqui. Não bloqueia o login acima, é só validação.
                # `add_email` não marca `primary=True` sozinho (ver
                # allauth/account/managers.py) — precisa do `set_as_primary`
                # explícito, senão o EmailAddress fica "órfão" sem primary.
                email_address = EmailAddress.objects.add_email(
                    request, user, user.email, confirm=True
                )
                email_address.set_as_primary()
                messages.success(
                    request,
                    f"Salão criado! Sua página pública é /{tenant.slug}/ — "
                    f"você já pode configurar tudo por aqui.",
                )
                return redirect("painel_home")
    return render(
        request,
        "painel/signup.html",
        {"form": form, "error": error, "detected_theme": detected_theme},
    )


# ---------------------------------------------------------------------------
# Escolha de tema pós-cadastro via Google — /painel/escolher-tema/
# ---------------------------------------------------------------------------


@tenant_admin_required(allow_when_theme_unconfirmed=True)
def choose_theme_view(request):
    """Primeiro acesso de quem se cadastrou via Google (sem passar pelo
    formulário de `/cadastrar/`, que já pede o tema) — `tenant_admin_required`
    redireciona pra cá enquanto `tenant.theme_confirmed` for False. Depois de
    confirmar aqui, o tema continua editável normalmente em Configurações."""
    tenant = request.tenant
    error = None
    if request.method == "POST":
        theme = request.POST.get("theme")
        if theme not in TenantTheme.values:
            error = "Escolha uma das opções."
        else:
            tenant.theme = theme
            tenant.theme_confirmed = True
            tenant.save(update_fields=["theme", "theme_confirmed"])
            messages.success(request, "Tema definido! Você pode trocar depois em Configurações.")
            return redirect("painel_home")
    return render(request, "painel/choose_theme.html", {"tenant": tenant, "error": error})


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
