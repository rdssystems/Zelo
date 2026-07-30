"""Integração com django-allauth — usada só pro login social (Google); o
login por e-mail/senha continua sendo o `LoginView` padrão do Django
(ver config/urls.py), sem depender do allauth.

Decisão do usuário (2026-07-30):
- E-mail da conta Google já existe como `User`: vincula automaticamente
  (login), sem passar pela tela de "esse e-mail já existe" do allauth.
- E-mail novo: cria tenant + user automaticamente, mesmo caminho de
  `register_tenant` usado no cadastro self-service (`/cadastrar/`) — sem
  senha (login sempre via Google; o dono pode trocar depois se quiser).
"""

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .models import User


class ZeloSocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        if sociallogin.is_existing:
            return
        email = sociallogin.user.email
        if not email:
            return
        try:
            existing = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return
        sociallogin.connect(request, existing)

    def save_user(self, request, sociallogin, form=None):
        """Só é chamado quando `pre_social_login` NÃO encontrou um `User`
        com esse e-mail — ou seja, é sempre um salão novo se cadastrando."""
        from apps.tenants.services import register_tenant

        email = sociallogin.user.email
        extra_data = sociallogin.account.extra_data or {}
        display_name = (
            extra_data.get("name") or extra_data.get("given_name") or email.split("@")[0]
        )

        _tenant, user = register_tenant(
            name=f"Salão de {display_name}",
            email=email,
            password=None,
        )
        sociallogin.user = user
        sociallogin.save(request)
        return user
