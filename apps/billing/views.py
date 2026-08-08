from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.accounts.decorators import superadmin_required, tenant_admin_required
from apps.accounts.models import User
from apps.tenants.forms import TenantDocumentForm
from apps.tenants.models import Tenant
from apps.tenants.services import delete_tenant_account

from . import asaas_client
from . import services as billing_ops
from .forms import (
    PlanForm,
    SubscriberDeleteForm,
    SubscriptionPeriodForm,
    SubscriptionPlanForm,
    SubscriptionStatusForm,
)
from .models import Plan, Subscription, SubscriptionStatus

# Conteúdo de marketing dos planos (não é dado de negócio, não precisa virar
# model — só 3 planos conhecidos, ver 01-REQUISITOS.md §4.2). Chave = nome do
# `Plan` no banco (seed em `apps/billing/migrations`).
PLAN_HIGHLIGHTS = {
    "Individual": [
        "Só você atendendo — sem conta de funcionário extra",
        "Estoque básico",
    ],
    "Profissional": [
        "Até 3 funcionários",
        "1 login por funcionário",
        "Estoque profissional completo (fornecedor, lote/validade, custo médio, inventário)",
        "Comissão automática por profissional",
        "CRM completo com histórico por cliente",
        "Pacotes de mensalidade",
        "Alerta de aniversário do cliente",
        "Relatórios avançados (em breve)",
    ],
    "Studio": [
        "Até 6 funcionários",
        "1 login por funcionário",
        "Estoque profissional completo (fornecedor, lote/validade, custo médio, inventário)",
        "Comissão automática por profissional",
        "CRM completo com histórico por cliente",
        "Pacotes de mensalidade",
        "Alerta de aniversário do cliente",
        "Relatórios avançados (em breve)",
    ],
}

PLAN_COMMON_BENEFITS = [
    "Agenda online 24h, sem precisar ligar pro salão",
    "Sem app pro cliente final — identificação só por telefone",
    "Cálculo de comissão automático, por funcionário e por serviço",
    "Caixa integrado a cada atendimento",
    "Cliente mensalista e carteira de crédito",
    "Cancelamento e reagendamento sem perder o horário do profissional",
    "Página pública personalizável (logo, capa, cores)",
    "Histórico e observações por cliente",
    "Dados isolados e seguros — um salão nunca vê dado de outro",
    "Backup diário automático dos seus dados",
    "Atualizações incluídas, sem custo extra",
]

# ---------------------------------------------------------------------------
# Dashboard — /plataforma/
# ---------------------------------------------------------------------------


@superadmin_required
def platform_dashboard(request):
    subscriptions = Subscription.objects.select_related("tenant", "plan")
    month_start = timezone.localdate().replace(day=1)

    mrr = subscriptions.filter(status=SubscriptionStatus.ACTIVE).aggregate(total=Sum("plan__price"))[
        "total"
    ] or 0

    context = {
        "active_nav": "dashboard",
        "total_tenants": Tenant.objects.count(),
        "active_count": subscriptions.filter(status=SubscriptionStatus.ACTIVE).count(),
        "trialing_count": subscriptions.filter(status=SubscriptionStatus.TRIALING).count(),
        "overdue_count": subscriptions.filter(status=SubscriptionStatus.OVERDUE).count(),
        "canceled_count": subscriptions.filter(status=SubscriptionStatus.CANCELED).count(),
        "mrr": mrr,
        "new_this_month": subscriptions.filter(created_at__date__gte=month_start).count(),
        "canceled_this_month": subscriptions.filter(
            status=SubscriptionStatus.CANCELED, updated_at__date__gte=month_start
        ).count(),
    }
    return render(request, "plataforma/dashboard.html", context)


# ---------------------------------------------------------------------------
# Planos — /plataforma/planos/
# ---------------------------------------------------------------------------


def _plan_items_response(request):
    items = render_to_string(
        "plataforma/plans/_items.html", {"plans": Plan.objects.all()}, request=request
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(items + modal_reset)


@superadmin_required
def plan_list(request):
    return render(
        request,
        "plataforma/plans/list.html",
        {"plans": Plan.objects.all(), "active_nav": "plans"},
    )


@superadmin_required
def plan_create(request):
    if request.method == "POST":
        form = PlanForm(request.POST)
        if form.is_valid():
            billing_ops.create_plan(**form.cleaned_data)
            return _plan_items_response(request)
    else:
        form = PlanForm(initial={"is_active": True})
    response = render(
        request, "plataforma/plans/_form.html", {"form": form, "title": "Novo Plano"}
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@superadmin_required
def plan_update(request, pk):
    plan = get_object_or_404(Plan, pk=pk)
    if request.method == "POST":
        form = PlanForm(request.POST)
        if form.is_valid():
            billing_ops.update_plan(plan, **form.cleaned_data)
            return _plan_items_response(request)
    else:
        form = PlanForm(
            initial={
                "name": plan.name,
                "description": plan.description,
                "price": plan.price,
                "is_active": plan.is_active,
                "order": plan.order,
            }
        )
    response = render(
        request,
        "plataforma/plans/_form.html",
        {"form": form, "title": "Editar Plano", "plan": plan},
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@superadmin_required
def plan_toggle(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    plan = get_object_or_404(Plan, pk=pk)
    plan.is_active = not plan.is_active
    plan.save(update_fields=["is_active"])
    return _plan_items_response(request)


# ---------------------------------------------------------------------------
# Assinantes — /plataforma/assinantes/
# ---------------------------------------------------------------------------


def _filtered_subscriptions(request):
    subscriptions = Subscription.objects.select_related("tenant", "plan").prefetch_related(
        Prefetch(
            "tenant__users",
            queryset=User.objects.filter(role=User.Role.TENANT_ADMIN),
            to_attr="admin_users",
        )
    )
    status = request.GET.get("status")
    if status:
        subscriptions = subscriptions.filter(status=status)
    plan_id = request.GET.get("plan")
    if plan_id:
        subscriptions = subscriptions.filter(plan_id=plan_id)
    q = request.GET.get("q")
    if q:
        subscriptions = subscriptions.filter(tenant__name__icontains=q)
    return subscriptions.order_by("tenant__name")


@superadmin_required
def subscriber_list(request):
    return render(
        request,
        "plataforma/subscribers/list.html",
        {
            "subscriptions": _filtered_subscriptions(request),
            "plans": Plan.objects.all(),
            "status_choices": SubscriptionStatus.choices,
            "selected_status": request.GET.get("status", ""),
            "selected_plan": request.GET.get("plan", ""),
            "q": request.GET.get("q", ""),
            "today": timezone.localdate(),
            "active_nav": "subscribers",
        },
    )


def _get_subscription(tenant_id):
    return get_object_or_404(
        Subscription.objects.select_related("tenant", "plan"), tenant_id=tenant_id
    )


@superadmin_required
def subscriber_detail(request, tenant_id):
    subscription = _get_subscription(tenant_id)
    period_initial = {
        "current_period_start": subscription.current_period_start,
        "current_period_end": subscription.current_period_end,
    }
    period_form = SubscriptionPeriodForm(initial=period_initial)
    if subscription.is_recurring:
        period_form.fields["current_period_start"].widget.attrs["disabled"] = True
        period_form.fields["current_period_end"].widget.attrs["disabled"] = True
    return render(
        request,
        "plataforma/subscribers/detail.html",
        {
            "subscription": subscription,
            "tenant": subscription.tenant,
            "plans": Plan.objects.filter(is_active=True),
            "status_choices": SubscriptionStatus.choices,
            "period_form": period_form,
            "active_nav": "subscribers",
        },
    )


@superadmin_required
def subscriber_change_plan(request, tenant_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    subscription = _get_subscription(tenant_id)
    form = SubscriptionPlanForm(request.POST)
    if form.is_valid():
        try:
            billing_ops.change_subscription_plan(subscription, form.cleaned_data["plan"])
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    return redirect("plataforma:subscriber_detail", tenant_id=tenant_id)


@superadmin_required
def subscriber_change_period(request, tenant_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    subscription = _get_subscription(tenant_id)
    form = SubscriptionPeriodForm(request.POST)
    if form.is_valid():
        try:
            billing_ops.update_subscription_period(
                subscription,
                current_period_start=form.cleaned_data["current_period_start"],
                current_period_end=form.cleaned_data["current_period_end"],
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "Período da assinatura atualizado.")
    else:
        messages.error(request, "Datas inválidas — confira o início e o fim do período.")
    return redirect("plataforma:subscriber_detail", tenant_id=tenant_id)


@superadmin_required
def subscriber_change_status(request, tenant_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    subscription = _get_subscription(tenant_id)
    form = SubscriptionStatusForm(request.POST)
    if form.is_valid():
        billing_ops.change_subscription_status(subscription, form.cleaned_data["status"])
    return redirect("plataforma:subscriber_detail", tenant_id=tenant_id)


@superadmin_required
def subscriber_toggle_active(request, tenant_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    tenant.is_active = not tenant.is_active
    tenant.save(update_fields=["is_active"])
    return redirect("plataforma:subscriber_detail", tenant_id=tenant_id)


@superadmin_required
def subscriber_delete_confirm(request, tenant_id):
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    return render(
        request,
        "plataforma/subscribers/_confirm_delete.html",
        {"tenant": tenant, "form": SubscriberDeleteForm(tenant_slug=tenant.slug)},
    )


@superadmin_required
def subscriber_delete(request, tenant_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    form = SubscriberDeleteForm(request.POST, tenant_slug=tenant.slug)
    if not form.is_valid():
        response = render(
            request,
            "plataforma/subscribers/_confirm_delete.html",
            {"tenant": tenant, "form": form},
        )
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
        return response
    delete_tenant_account(tenant)
    response = HttpResponse()
    response.headers["HX-Redirect"] = reverse("plataforma:subscriber_list")
    return response


# ---------------------------------------------------------------------------
# Painel do tenant — /painel/plano/
# ---------------------------------------------------------------------------


@tenant_admin_required(allow_when_blocked=True)
def my_plan(request):
    subscription = Subscription.objects.select_related("plan").get(tenant=request.tenant)
    plan_cards = [
        {"plan": plan, "highlights": PLAN_HIGHLIGHTS.get(plan.name, [])}
        for plan in Plan.objects.filter(is_active=True)
    ]
    days_left = None
    if subscription.status != SubscriptionStatus.ACTIVE and subscription.trial_ends_at:
        # `.date()` direto num datetime aware extrai a data em UTC, não na
        # TIME_ZONE do projeto — usar `localtime()` primeiro (mesmo ajuste
        # em `apps/billing/context_processors.py::sidebar_plan`).
        days_left = max(
            0, (timezone.localtime(subscription.trial_ends_at).date() - timezone.localdate()).days
        )
    grace_days_left = None
    if subscription.status == SubscriptionStatus.OVERDUE:
        deadline = billing_ops.grace_deadline(subscription)
        if deadline is not None:
            grace_days_left = (deadline - timezone.localdate()).days
    # Histórico de transação (2026-08-08) só custa a chamada ao Asaas pra
    # quem realmente tem cobrança recorrente ativa — não faz sentido buscar
    # em trial/pending/overdue sem asaas_subscription_id.
    transaction_history = None
    if subscription.status == SubscriptionStatus.ACTIVE and subscription.is_recurring:
        transaction_history = billing_ops.get_transaction_history(subscription)
    return render(
        request,
        "painel/billing/my_plan.html",
        {
            "active_nav": "plan",
            "subscription": subscription,
            "plan_cards": plan_cards,
            "common_benefits": PLAN_COMMON_BENEFITS,
            "days_left": days_left,
            "grace_days_left": grace_days_left,
            "panel_blocked": billing_ops.subscription_blocks_panel_access(request.tenant),
            "show_welcome": request.GET.get("ativado") == "1",
            "transaction_history": transaction_history,
        },
    )


@tenant_admin_required(allow_when_blocked=True)
def cancel_subscription_confirm(request):
    subscription = Subscription.objects.select_related("plan").get(tenant=request.tenant)
    period_end = subscription.current_period_end
    if period_end:
        message = (
            f"Sua assinatura do plano {subscription.plan.name} deixa de renovar — "
            f"nenhuma nova cobrança será gerada. Você continua com acesso normal ao "
            f"painel até {period_end.strftime('%d/%m/%Y')} (fim do período já pago)."
        )
    else:
        message = (
            f"Sua assinatura do plano {subscription.plan.name} deixa de renovar — "
            "nenhuma nova cobrança será gerada."
        )
    return render(
        request,
        "painel/_confirm_action.html",
        {
            "title": "Cancelar assinatura",
            "message": message,
            "icon": "cancel",
            "confirm_label": "Cancelar assinatura",
            "action_url": reverse("billing:cancel_subscription"),
            "target": "#modal-slot",
        },
    )


@tenant_admin_required(allow_when_blocked=True)
def cancel_subscription_view(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    subscription = Subscription.objects.select_related("plan").get(tenant=request.tenant)
    try:
        billing_ops.cancel_subscription(subscription)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    except asaas_client.AsaasError as exc:
        messages.error(request, f"Não deu pra cancelar agora, tenta de novo em instantes: {exc}")
    else:
        messages.success(
            request,
            "Assinatura cancelada — você continua com acesso normal até o fim do período "
            "já pago, sem nenhuma cobrança nova depois disso.",
        )
    response = HttpResponse()
    response.headers["HX-Redirect"] = reverse("billing:my_plan")
    return response


@tenant_admin_required(allow_when_blocked=True)
def select_plan_view(request, plan_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    plan = get_object_or_404(Plan, pk=plan_id, is_active=True)
    subscription = Subscription.objects.get(tenant=request.tenant)
    try:
        billing_ops.select_plan(subscription, plan)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("billing:my_plan")
    return redirect("billing:checkout")


@tenant_admin_required(allow_when_blocked=True)
def checkout_view(request):
    subscription = Subscription.objects.select_related("plan").get(tenant=request.tenant)
    if not subscription.plan:
        return redirect("billing:my_plan")

    invoice_url = None
    document_form = None
    error = None
    try:
        invoice_url = billing_ops.get_or_create_checkout_url(
            subscription, admin_email=request.user.email
        )
    except ValidationError as exc:
        if not request.tenant.document:
            document_form = TenantDocumentForm(instance=request.tenant)
        else:
            error = " ".join(exc.messages)
    except asaas_client.AsaasNotConfigured:
        error = (
            "O pagamento online ainda não foi ativado pela plataforma "
            "(credenciais do Asaas pendentes) — fale com o suporte pra assinar."
        )
    except asaas_client.AsaasError as exc:
        error = str(exc)

    return render(
        request,
        "painel/billing/checkout.html",
        {
            "active_nav": "plan",
            "subscription": subscription,
            "invoice_url": invoice_url,
            "document_form": document_form,
            "error": error,
        },
    )


@tenant_admin_required(allow_when_blocked=True)
def submit_document_view(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    form = TenantDocumentForm(request.POST, instance=request.tenant)
    if form.is_valid():
        form.save()
    else:
        messages.error(request, " ".join(form.errors.get("document", ["CPF/CNPJ inválido."])))
    return redirect("billing:checkout")


@tenant_admin_required(allow_when_blocked=True)
def checkout_status(request):
    """Polling HTMX (a cada poucos segundos, ver checkout.html) enquanto a
    confirmação do pagamento não chega pelo webhook — evita o admin do salão
    ficar preso numa tela sem saber se já pagou."""
    subscription = Subscription.objects.select_related("plan").get(tenant=request.tenant)
    if subscription.status == SubscriptionStatus.ACTIVE:
        response = render(
            request, "painel/billing/_status_pill.html", {"subscription": subscription}
        )
        response.headers["HX-Redirect"] = reverse("billing:my_plan") + "?ativado=1"
        return response
    return render(request, "painel/billing/_status_pill.html", {"subscription": subscription})
