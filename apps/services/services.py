"""Operações de domínio do catálogo de serviços.

Toda regra de negócio de Service vive aqui (CLAUDE.md) — views/viewsets apenas
chamam estas funções.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import ProtectedError

from apps.inventory.models import WHOLE_UNIT_CODES

from .models import Service, ServiceProduct


def _validate(name, duration_minutes, price):
    if not str(name).strip():
        raise ValidationError({"name": "O nome do serviço é obrigatório."})
    if int(duration_minutes) < 1:
        raise ValidationError(
            {"duration_minutes": "A duração deve ser de pelo menos 1 minuto."}
        )
    if Decimal(price) < 0:
        raise ValidationError({"price": "O preço não pode ser negativo."})


def create_service(*, tenant, name, duration_minutes, price, description=""):
    _validate(name, duration_minutes, price)
    return Service.objects.create(
        tenant=tenant,
        name=str(name).strip(),
        description=description or "",
        duration_minutes=duration_minutes,
        price=price,
    )


def update_service(service, *, name, duration_minutes, price, description=""):
    _validate(name, duration_minutes, price)
    service.name = str(name).strip()
    service.description = description or ""
    service.duration_minutes = duration_minutes
    service.price = price
    service.save(
        update_fields=["name", "description", "duration_minutes", "price"]
    )
    return service


def set_service_active(service, is_active):
    service.is_active = bool(is_active)
    service.save(update_fields=["is_active"])
    return service


def delete_service(service):
    """Exclusão definitiva — o modal de confirmação do painel é a barreira
    contra clique acidental (não exige mais desativar antes; pedido do
    usuário em 2026-07-29). A proteção real contra perda de histórico é o
    PROTECT abaixo."""
    try:
        service.delete()
    except ProtectedError:
        # Appointment.service é PROTECT (histórico de agendamento não pode
        # sumir junto com o serviço) — ver apps/scheduling/models.py
        raise ValidationError(
            "Não é possível excluir: existem agendamentos vinculados a este serviço. "
            "Desative-o para que deixe de ser oferecido, mantendo o histórico."
        )


def bookable_services(tenant):
    """Serviços exibidos na página pública (RF14).

    Regra: ativo E com ao menos um funcionário ATIVO vinculado
    (vínculo = EmployeeService, ver apps/employees).
    """
    return (
        Service.objects.for_tenant(tenant)
        .filter(is_active=True, employee_links__employee__is_active=True)
        .distinct()
    )


def _validate_recipe_item(service, product, quantity):
    # Regra #1 do CLAUDE.md — isolamento multi-tenant é inegociável (mesma
    # checagem que apps.scheduling.services.create_appointment faz pros
    # FKs de um Appointment).
    if product.tenant_id != service.tenant_id:
        raise ValidationError({"product": "Produto não pertence a este tenant."})
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise ValidationError({"quantity": "A quantidade deve ser maior que zero."})
    if product.unit in WHOLE_UNIT_CODES and quantity != quantity.to_integral_value():
        raise ValidationError(
            {
                "quantity": (
                    f"Quantidade de \"{product.name}\" deve ser um número inteiro "
                    f"({product.get_unit_display()} não aceita fração)."
                )
            }
        )
    return quantity


def add_recipe_item(*, service, product, quantity):
    """RF48 — adiciona (ou atualiza a quantidade de) um insumo na receita do
    serviço. Consumido automaticamente a cada atendimento concluído, sem
    cobrar o cliente — ver apps.scheduling.services.complete_appointment."""
    quantity = _validate_recipe_item(service, product, quantity)
    item, _ = ServiceProduct.objects.update_or_create(
        tenant=service.tenant, service=service, product=product,
        defaults={"quantity": quantity},
    )
    return item


def set_recipe_item_quantity(item, quantity):
    quantity = _validate_recipe_item(item.service, item.product, quantity)
    item.quantity = quantity
    item.save(update_fields=["quantity"])
    return item


def remove_recipe_item(item):
    item.delete()
