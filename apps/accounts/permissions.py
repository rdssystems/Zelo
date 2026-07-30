"""Permissões DRF por role + tenant (ver 02-ARQUITETURA.md §5)."""

from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsTenantMember(BasePermission):
    """Usuário autenticado e vinculado a um tenant (admin do salão ou funcionário)."""

    message = "Você precisa estar vinculado a um salão."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.tenant_id)


class IsTenantAdminOrReadOnly(BasePermission):
    """Leitura para qualquer membro do tenant; escrita apenas para o admin do salão."""

    message = "Apenas o administrador do salão pode alterar este recurso."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        return bool(
            user and user.is_authenticated and user.role == "tenant_admin"
        )
