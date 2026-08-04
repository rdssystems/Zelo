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
    """`LoginView` padrão do Django — decisão do usuário em 2026-08-03:
    `/painel/login/` é a mesma URL em todo host, mas o subdomínio
    (`barbearia.`/`salao.`) decide qual tema é renderizado (ver
    `entrance_theme`/`entrance_base`, `apps/tenants/context_processors.py` —
    disponíveis em `templates/painel/login.html` sem precisar de contexto
    próprio aqui)."""

    template_name = "painel/login.html"
