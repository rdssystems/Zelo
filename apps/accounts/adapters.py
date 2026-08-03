"""Integração com django-allauth — usada só pro login social (Google); o
login por e-mail/senha continua sendo o `LoginView` padrão do Django
(ver config/urls.py), sem depender do allauth.

Decisão do usuário (2026-07-30):
- E-mail da conta Google já existe como `User`: vincula automaticamente
  (login), sem passar pela tela de "esse e-mail já existe" do allauth.
- E-mail novo: cria tenant + user automaticamente, mesmo caminho de
  `register_tenant` usado no cadastro self-service (`/cadastrar/`) — sem
  senha (login sempre via Google; o dono pode trocar depois se quiser).

Decisão do usuário (2026-08-02): esse caminho não tem formulário pra
escolher o tema (Salão de Beleza/Barbearia) — nasce com `theme_confirmed=
False`, o que faz `apps.accounts.decorators.tenant_admin_required`
redirecionar o primeiro acesso pra `painel/escolher-tema/` antes de
liberar o resto do painel.

Decisão do usuário (2026-08-03): quando o login via Google acontece num
dos subdomínios dedicados (`barbearia.zellup.com.br` / `salao.zellup.
com.br`), o tema já é conhecido pelo host — nesse caso nasce direto com
`theme_confirmed=True` e pula a tela de escolha. Fora desses subdomínios
(dev local, domínio raiz), continua o comportamento acima.
"""

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .models import User


class ZellupSocialAccountAdapter(DefaultSocialAccountAdapter):
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
        from apps.tenants.models import TenantTheme
        from apps.tenants.services import register_tenant, theme_from_host

        email = sociallogin.user.email
        extra_data = sociallogin.account.extra_data or {}
        display_name = (
            extra_data.get("name") or extra_data.get("given_name") or email.split("@")[0]
        )

        detected_theme = theme_from_host(request.get_host())
        establishment = "Barbearia" if detected_theme == TenantTheme.BARBEARIA else "Salão"

        _tenant, user = register_tenant(
            name=f"{establishment} de {display_name}",
            email=email,
            password=None,
            theme=detected_theme or TenantTheme.SALAO,
            theme_confirmed=bool(detected_theme),
        )
        sociallogin.user = user
        sociallogin.save(request)
        return user
