from django.shortcuts import get_object_or_404

from .models import Tenant


class TenantMiddleware:
    """Resolve o tenant atual e injeta em `request.tenant`.

    Regras de resolução:
    - Painel logado: `request.user.tenant` (admin do salão ou funcionário).
    - Página pública: slug na URL — qualquer view cujo kwarg de URL seja
      `tenant_slug` tem o tenant resolvido (404 se inexistente ou inativo).
      O slug tem precedência: um usuário logado de um tenant visitando a
      página pública de outro vê o tenant da URL, não o seu.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.tenant_id:
            request.tenant = user.tenant
        else:
            request.tenant = None
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        slug = view_kwargs.get("tenant_slug")
        if slug:
            request.tenant = get_object_or_404(Tenant, slug=slug, is_active=True)
        return None
