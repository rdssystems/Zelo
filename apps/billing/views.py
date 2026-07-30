from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.accounts.decorators import superadmin_required
from apps.accounts.models import User
from apps.tenants.models import Tenant
from apps.tenants.services import delete_tenant_account

from . import services as billing_ops
from .forms import (
    PlanForm,
    SubscriberDeleteForm,
    SubscriptionPeriodForm,
    SubscriptionPlanForm,
    SubscriptionStatusForm,
)
from .models import Plan, Subscription, SubscriptionStatus

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
