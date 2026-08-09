from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import JsonResponse
from django.urls import include, path
from django.views.generic import RedirectView, TemplateView
from rest_framework.routers import DefaultRouter

from apps.accounts.views import ZellupLoginView, painel_home
from apps.billing.views_webhook import asaas_webhook
from apps.clients.views import ClientViewSet
from apps.employees.views import EmployeeViewSet
from apps.finance.views import CashTransactionViewSet, CommissionViewSet, my_commissions
from apps.inventory.views import ProductViewSet, StockMovementViewSet, SupplierViewSet
from apps.scheduling.views import AppointmentViewSet, my_agenda
from apps.services.views import ServiceViewSet
from apps.tenants.views import TenantSettingsView, choose_theme_view, landing_view, signup_view


def healthz(request):
    return JsonResponse({"status": "ok"})


router = DefaultRouter()
router.register("services", ServiceViewSet, basename="service")
router.register("employees", EmployeeViewSet, basename="employee")
router.register("products", ProductViewSet, basename="product")
router.register("suppliers", SupplierViewSet, basename="supplier")
router.register("stock-movements", StockMovementViewSet, basename="stockmovement")
router.register("appointments", AppointmentViewSet, basename="appointment")
router.register("cash-transactions", CashTransactionViewSet, basename="cashtransaction")
router.register("commissions", CommissionViewSet, basename="commission")
router.register("clients", ClientViewSet, basename="client")

urlpatterns = [
    path("", landing_view, name="landing"),
    path("superadmin/", admin.site.urls),
    path("healthz/", healthz),
    # PWA: service worker precisa ser servido na raiz (não em /static/) pra
    # ter escopo no site inteiro — ver templates/sw.js.
    path(
        "sw.js",
        TemplateView.as_view(template_name="sw.js", content_type="application/javascript"),
        name="service_worker",
    ),
    path("offline/", TemplateView.as_view(template_name="offline.html"), name="offline"),
    # Painel
    path("painel/login/", ZellupLoginView.as_view(), name="login"),
    path("painel/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("painel/", painel_home, name="painel_home"),
    path("painel/escolher-tema/", choose_theme_view, name="choose_theme"),
    path(
        "painel/conta-excluida/",
        TemplateView.as_view(template_name="painel/account_deleted.html"),
        name="account_deleted",
    ),
    path("cadastrar/", signup_view, name="signup"),
    # django-allauth — só o provedor Google é usado (ver
    # apps/accounts/adapters.py); login por e-mail/senha continua sendo o
    # LoginView acima, sem depender do allauth.
    path("accounts/", include("allauth.urls")),
    path("termos/", TemplateView.as_view(template_name="legal/terms.html"), name="terms"),
    path("privacidade/", TemplateView.as_view(template_name="legal/privacy.html"), name="privacy"),
    path("painel/servicos/", include("apps.services.urls")),
    path("painel/funcionarios/", include("apps.employees.urls")),
    path("painel/estoque/", include("apps.inventory.urls")),
    path("painel/clientes/", include("apps.clients.urls")),
    # Dashboard foi incorporado em Relatórios (aba "Visão Geral") em
    # 2026-08-05 — redirect pra não quebrar bookmark/link antigo.
    path("painel/dashboard/", RedirectView.as_view(pattern_name="reports:index", permanent=False)),
    path("painel/relatorios/", include("apps.reports.urls")),
    path("painel/agenda/", include("apps.scheduling.urls")),
    path("painel/caixa/", include("apps.finance.urls")),
    path("painel/configuracoes/", include("apps.tenants.urls")),
    path("painel/avisos/", include("apps.notifications.urls")),
    path("painel/plano/", include("apps.billing.urls_tenant")),
    # Webhook do Asaas — fora de /painel/ e /plataforma/ (chamado pelo Asaas,
    # não por usuário logado; autenticação é via header, não sessão).
    path("webhooks/asaas/", asaas_webhook, name="asaas_webhook"),
    # Painel do superadmin (custom) — /superadmin/ acima continua sendo o
    # Django Admin cru, por instrução explícita do usuário; este é o painel
    # próprio (planos, assinantes, dashboard da plataforma, avisos). A rota
    # de avisos vem ANTES da raiz de plataforma/ pra não depender de
    # fallthrough de resolução de URL entre os dois include()s.
    path("plataforma/avisos/", include("apps.notifications.urls_admin")),
    path("plataforma/", include("apps.billing.urls")),
    # Autoatendimento do funcionário (RF12) — fora dos apps admin-only acima
    path("painel/minha-agenda/", my_agenda, name="my_agenda"),
    path("painel/minha-comissao/", my_commissions, name="my_commissions"),
    # API REST (futuro app mobile)
    path("api/v1/", include(router.urls)),
    path("api/v1/tenant-settings/", TenantSettingsView.as_view(), name="tenant-settings"),
    # Página pública do tenant — catch-all de 1 segmento, deve ficar por
    # último (senão engoliria "painel/", "api/" etc). RESERVED_TENANT_SLUGS
    # em apps/tenants/models.py impede um salão de escolher esses nomes.
    path("<slug:tenant_slug>/", include("apps.public.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
