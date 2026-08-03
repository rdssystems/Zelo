from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect


def _panel_access_blocked(request):
    """RF30 — bloqueio por inadimplência (`apps.billing.services.
    subscription_blocks_panel_access`). Import local pra não acoplar
    `apps.accounts` a `apps.billing` no carregamento (mesmo padrão de
    import tardio já usado entre apps.clients/apps.scheduling)."""
    from apps.billing.services import subscription_blocks_panel_access

    return subscription_blocks_panel_access(request.tenant)


def tenant_admin_required(view_func=None, *, allow_when_blocked=False):
    """Restringe a view de painel ao admin do salão (role=tenant_admin com tenant).

    `allow_when_blocked=True` isenta a view do bloqueio RF30 (assinatura
    `overdue` além do prazo de tolerância, ou `canceled`) — usado só nas
    telas de `apps.billing` que o admin precisa pra regularizar (Meu Plano,
    checkout, envio de CPF/CNPJ, polling de status).
    """

    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapper(request, *args, **kwargs):
            user = request.user
            if user.role != "tenant_admin" or request.tenant is None:
                return HttpResponseForbidden(
                    "Acesso restrito ao administrador do salão."
                )
            if not allow_when_blocked and _panel_access_blocked(request):
                messages.warning(
                    request,
                    "A assinatura do salão está com o pagamento pendente — regularize "
                    "para voltar a usar o painel normalmente.",
                )
                return redirect("billing:my_plan")
            return fn(request, *args, **kwargs)

        return wrapper

    if view_func is not None:
        return decorator(view_func)
    return decorator


def superadmin_required(view_func):
    """Restringe a view ao superadmin da plataforma (painel /plataforma/, não
    o Django Admin em /superadmin/ — esse usa a própria auth do admin.site)."""

    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if request.user.role != "superadmin":
            return HttpResponseForbidden(
                "Acesso restrito à administração da plataforma."
            )
        return view_func(request, *args, **kwargs)

    return wrapper


def employee_required(view_func):
    """Restringe a view ao funcionário logado (precisa ter um Employee vinculado).

    Usada nas telas de autoatendimento do funcionário (`/painel/minha-agenda/`,
    `/painel/minha-comissao/`) — nunca expõem dado de outro funcionário nem do
    tenant como um todo (02-ARQUITETURA.md §5).

    Também sujeita ao bloqueio RF30 — só o admin do salão consegue ver/agir
    em `/painel/plano/`, então um funcionário bloqueado recebe 403 (não tem
    pra onde redirecionar) com uma mensagem explicando o motivo.
    """

    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not hasattr(request.user, "employee_profile") or request.tenant is None:
            return HttpResponseForbidden(
                "Acesso restrito a funcionários vinculados a um salão."
            )
        if _panel_access_blocked(request):
            return HttpResponseForbidden(
                "A assinatura do salão está com o pagamento pendente — fale com o "
                "administrador do salão para regularizar."
            )
        return view_func(request, *args, **kwargs)

    return wrapper
