from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect

from .models import User


@login_required
def painel_home(request):
    """Ponto de entrada do painel — cada papel tem sua home."""
    if request.user.role == User.Role.EMPLOYEE:
        return redirect("my_agenda")
    if request.user.role == User.Role.SUPERADMIN:
        return redirect("plataforma:dashboard")
    return redirect("services:list")


class ZellupLoginView(LoginView):
    """`LoginView` padrão do Django, só injetando `theme` no contexto —
    decisão do usuário em 2026-08-03: `/painel/login/` é a mesma URL em
    todo host, mas o subdomínio (`barbearia.`/`salao.`) decide qual das 2
    telas (ver `templates/painel/login.html`) é renderizada. Fora dos
    subdomínios conhecidos, `theme_from_host` retorna `None` e o template
    cai no visual padrão (salão)."""

    template_name = "painel/login.html"

    def get_context_data(self, **kwargs):
        from apps.tenants.services import theme_from_host

        context = super().get_context_data(**kwargs)
        context["theme"] = theme_from_host(self.request.get_host())
        return context
