from django.contrib.auth.decorators import login_required
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
