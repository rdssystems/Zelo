"""Operações de domínio de tenant — cadastro self-service do salão.

Decisão tomada com o usuário em 2026-07-19: cada tenant é 1 salão; o dono se
cadastra sozinho em `/cadastrar/` e vira `tenant_admin` do salão recém-criado.
Sem etapa de aprovação/billing ainda (Etapa 9/Asaas cuida disso depois) — o
tenant já nasce ativo.
"""

import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import slugify

from .models import RESERVED_TENANT_SLUGS, Tenant, TenantBusinessHours, Weekday

User = get_user_model()

# Padrão sugerido pro tenant novo (o admin ajusta depois em Configurações) —
# Seg-Sex 9h-19h, Sáb 9h-13h, Dom fechado.
_DEFAULT_BUSINESS_HOURS = {
    Weekday.MONDAY: (False, datetime.time(9, 0), datetime.time(19, 0)),
    Weekday.TUESDAY: (False, datetime.time(9, 0), datetime.time(19, 0)),
    Weekday.WEDNESDAY: (False, datetime.time(9, 0), datetime.time(19, 0)),
    Weekday.THURSDAY: (False, datetime.time(9, 0), datetime.time(19, 0)),
    Weekday.FRIDAY: (False, datetime.time(9, 0), datetime.time(19, 0)),
    Weekday.SATURDAY: (False, datetime.time(9, 0), datetime.time(13, 0)),
    Weekday.SUNDAY: (True, None, None),
}


def _generate_unique_slug(name):
    base = slugify(name)[:50] or "salao"
    slug = base
    suffix = 1
    while slug in RESERVED_TENANT_SLUGS or Tenant.objects.filter(slug=slug).exists():
        suffix += 1
        slug = f"{base}-{suffix}"
    return slug


def create_default_business_hours(tenant):
    """Cria os 7 dias da semana com um horário padrão sugerido — o painel de
    Configurações sempre espera encontrar exatamente 1 linha por dia."""
    TenantBusinessHours.objects.bulk_create(
        TenantBusinessHours(
            tenant=tenant, weekday=weekday, is_closed=is_closed,
            start_time=start_time, end_time=end_time,
        )
        for weekday, (is_closed, start_time, end_time) in _DEFAULT_BUSINESS_HOURS.items()
    )


@transaction.atomic
def set_business_hours(tenant, days):
    """`days`: iterável de dicts com `weekday`/`is_closed`/`start_time`/`end_time`
    pros 7 dias da semana — substitui (upsert por dia) de uma vez só."""
    for day in days:
        weekday_label = Weekday(day["weekday"]).label
        is_closed = day["is_closed"]
        start_time = None if is_closed else day.get("start_time")
        end_time = None if is_closed else day.get("end_time")
        if not is_closed:
            if not start_time or not end_time:
                raise ValidationError(
                    f"Informe abertura e fechamento para {weekday_label}, ou marque como fechado."
                )
            if start_time >= end_time:
                raise ValidationError(
                    f"O fechamento deve ser depois da abertura em {weekday_label}."
                )
        TenantBusinessHours.objects.update_or_create(
            tenant=tenant,
            weekday=day["weekday"],
            defaults={
                "is_closed": is_closed,
                "start_time": start_time,
                "end_time": end_time,
            },
        )


@transaction.atomic
def register_tenant(*, name, email, password):
    """Cria o `Tenant` e o `User` (role=tenant_admin) do dono do salão.

    `password=None` cria o usuário com senha inutilizável (`set_password`
    trata `None` assim) — usado pelo cadastro automático via Google
    (`apps.accounts.adapters.ZeloSocialAccountAdapter.save_user`), onde o
    login é sempre via OAuth, nunca por senha."""
    name = str(name).strip()
    if not name:
        raise ValidationError({"name": "O nome do estabelecimento é obrigatório."})
    if User.objects.filter(email__iexact=email).exists():
        raise ValidationError({"email": "Já existe uma conta com este e-mail."})

    tenant = Tenant.objects.create(name=name, slug=_generate_unique_slug(name))
    user = User.objects.create_user(
        email=email,
        password=password,
        role=User.Role.TENANT_ADMIN,
        tenant=tenant,
    )
    create_default_business_hours(tenant)
    # Import local pra evitar import circular (apps.billing.models importa
    # apps.tenants.models.Tenant) — todo tenant novo já nasce com uma
    # Subscription em teste, sem plano, pro superadmin ver na lista de
    # assinantes em /plataforma/ mesmo antes de atribuir um plano.
    from apps.billing.services import create_subscription_for_tenant

    create_subscription_for_tenant(tenant)
    return tenant, user


@transaction.atomic
def delete_tenant_account(tenant):
    """Exclusão definitiva e irreversível da conta do salão inteiro.

    Várias FKs no projeto usam `PROTECT` (Appointment→Employee/Service/Client,
    CashTransaction/Commission→Appointment, StockMovement→Product...) para
    impedir a exclusão ACIDENTAL de um registro isolado — isso é correto e
    intencional nas Etapas 2/4/6/7. Só que o Django não resolve PROTECT nem
    quando os dois lados estão sendo apagados juntos (testado empiricamente:
    `tenant.delete()` puro estoura `ProtectedError`), então uma exclusão de
    CONTA inteira, deliberada e confirmada pelo dono, precisa apagar tudo na
    ordem inversa de dependência manualmente.
    """
    from apps.clients.models import Client, ClientCreditTransaction
    from apps.employees.models import Employee
    from apps.finance.models import CashTransaction, Commission
    from apps.inventory.models import PhysicalInventoryCount, Product, ProductBatch, StockMovement
    from apps.scheduling.models import Appointment
    from apps.services.models import Service

    # ClientCreditTransaction protege Client, Appointment e CashTransaction —
    # tem que sair primeiro.
    ClientCreditTransaction.objects.filter(tenant=tenant).delete()
    CashTransaction.objects.filter(tenant=tenant).delete()
    Commission.objects.filter(tenant=tenant).delete()
    # StockMovement.delete() cascade-apaga StockMovementBatch (FK CASCADE)
    # junto — só ProductBatch (PROTECT em Product) precisa sair à parte.
    StockMovement.objects.filter(tenant=tenant).delete()
    ProductBatch.objects.filter(tenant=tenant).delete()
    # PhysicalInventoryCount.delete() cascade-apaga PhysicalInventoryCountItem
    # (FK CASCADE) — que é quem protege Product (PROTECT).
    PhysicalInventoryCount.objects.filter(tenant=tenant).delete()
    Appointment.objects.filter(tenant=tenant).delete()
    Client.objects.filter(tenant=tenant).delete()

    # Employee.user é quem cascade-deleta o Employee (mesmo caminho de
    # apps/employees/services.py::delete_employee) — remove o login junto.
    for employee in Employee.objects.filter(tenant=tenant):
        employee.user.delete()

    Service.objects.filter(tenant=tenant).delete()
    Product.objects.filter(tenant=tenant).delete()

    # usuário(s) restante(s) do tenant — normalmente só o(s) tenant_admin
    User.objects.filter(tenant=tenant).delete()

    tenant.delete()
