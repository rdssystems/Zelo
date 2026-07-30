from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden


def tenant_admin_required(view_func):
    """Restringe a view de painel ao admin do salão (role=tenant_admin com tenant)."""

    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        user = request.user
        if user.role != "tenant_admin" or request.tenant is None:
            return HttpResponseForbidden(
                "Acesso restrito ao administrador do salão."
            )
        return view_func(request, *args, **kwargs)

    return wrapper


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
    """

    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not hasattr(request.user, "employee_profile") or request.tenant is None:
            return HttpResponseForbidden(
                "Acesso restrito a funcionários vinculados a um salão."
            )
        return view_func(request, *args, **kwargs)

    return wrapper
