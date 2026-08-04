"""Operações de domínio de funcionários, jornada e vínculo com serviços.

Regra de comissão (CLAUDE.md, regra 4):
prioridade `EmployeeService` (override por serviço) > `Employee` (padrão).
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import ProtectedError

from .models import CommissionType, Employee, EmployeeService, WorkingHours

User = get_user_model()


def _to_decimal(value):
    """Aceita vírgula OU ponto como separador decimal (entrada vem direto do
    POST bruto do template de vínculo de serviços, sem passar por um Form)."""
    if isinstance(value, str) and "," in value:
        value = value.strip().replace(".", "").replace(",", ".")
    return Decimal(value)


def _validate_commission(commission_type, commission_value):
    if commission_type not in CommissionType.values:
        raise ValidationError({"commission_type": "Tipo de comissão inválido."})
    value = _to_decimal(commission_value)
    if value < 0:
        raise ValidationError(
            {"commission_value": "O valor da comissão não pode ser negativo."}
        )
    if commission_type == CommissionType.PERCENTAGE and value > 100:
        raise ValidationError(
            {"commission_value": "Percentual de comissão não pode passar de 100%."}
        )


def _send_welcome_email(employee):
    # MVP: backend de console em dev — em produção passa a SMTP via .env
    # A senha é definida pelo admin na hora do cadastro e NUNCA trafega por
    # e-mail em texto puro.
    send_mail(
        subject="Seu acesso ao Zellup",
        message=(
            f"Olá, {employee.full_name}!\n\n"
            f"Seu acesso ao painel do salão {employee.tenant.name} foi criado.\n\n"
            f"Login: {employee.user.email}\n\n"
            f"Peça a senha de acesso a quem cadastrou você no sistema."
        ),
        from_email=None,
        recipient_list=[employee.user.email],
        fail_silently=False,
    )


def _validate_new_password(password):
    try:
        validate_password(password)
    except ValidationError as exc:
        raise ValidationError({"password": exc.messages})


@transaction.atomic
def create_employee(
    *,
    tenant,
    full_name,
    email,
    password,
    default_commission_type,
    default_commission_value,
    phone="",
    bio="",
    photo=None,
):
    """Cria o funcionário e, automaticamente, seu User de login (RF08).

    A senha é escolhida pelo admin no momento do cadastro (não é gerada
    nem enviada por e-mail — evita depender de SMTP configurado)."""
    from apps.billing.services import assert_can_add_employee

    assert_can_add_employee(tenant)

    full_name = str(full_name).strip()
    if not full_name:
        raise ValidationError({"full_name": "O nome é obrigatório."})
    _validate_commission(default_commission_type, default_commission_value)
    if User.objects.filter(email__iexact=email).exists():
        raise ValidationError(
            {"email": "Já existe um usuário com este e-mail."}
        )
    _validate_new_password(password)

    user = User.objects.create_user(
        email=email,
        password=password,
        role=User.Role.EMPLOYEE,
        tenant=tenant,
    )
    employee = Employee.objects.create(
        tenant=tenant,
        user=user,
        full_name=full_name,
        phone=phone or "",
        bio=bio or "",
        photo=photo,
        default_commission_type=default_commission_type,
        default_commission_value=default_commission_value,
    )
    _send_welcome_email(employee)
    return employee


def update_employee(
    employee,
    *,
    full_name,
    default_commission_type,
    default_commission_value,
    phone="",
    bio="",
    photo=None,
    password=None,
):
    full_name = str(full_name).strip()
    if not full_name:
        raise ValidationError({"full_name": "O nome é obrigatório."})
    _validate_commission(default_commission_type, default_commission_value)
    employee.full_name = full_name
    employee.phone = phone or ""
    employee.bio = bio or ""
    if photo is not None:
        employee.photo = photo
    employee.default_commission_type = default_commission_type
    employee.default_commission_value = default_commission_value
    employee.save()
    if password:
        _validate_new_password(password)
        employee.user.set_password(password)
        employee.user.save(update_fields=["password"])
    return employee


@transaction.atomic
def set_employee_active(employee, is_active):
    """Ativa/desativa o profissional e sincroniza o login (inativo não entra).

    Exceção: o perfil do responsável (`employee.is_owner`) reaproveita o
    login de admin do tenant — nunca desativamos esse `User` por aqui, senão
    o dono ficaria trancado pra fora do próprio painel. Quem liga/desliga
    esse perfil é o checkbox "também atende" em Configurações
    (`sync_owner_employee`), não o toggle da lista de funcionários.

    Reativar (False → True) alguém que não é o dono volta a ocupar vaga do
    plano — mesma trava de `create_employee` (ver
    `apps.billing.services.assert_can_add_employee`).
    """
    if is_active and not employee.is_active and not employee.is_owner:
        from apps.billing.services import assert_can_add_employee

        assert_can_add_employee(employee.tenant)

    employee.is_active = bool(is_active)
    employee.save(update_fields=["is_active"])
    if not employee.is_owner:
        employee.user.is_active = bool(is_active)
        employee.user.save(update_fields=["is_active"])
    return employee


def delete_employee(employee):
    """Exclusão definitiva do funcionário. Só permitida já inativo.

    Remove também o `User` de login (o `Employee` cascadeia junto, via
    `on_delete=CASCADE` no FK `Employee.user`) — não deixamos login órfão.
    """
    if employee.is_owner:
        raise ValidationError(
            "Não é possível excluir o perfil do responsável. Desmarque "
            '"também atende" em Configurações.'
        )
    if employee.is_active:
        raise ValidationError("Desative o funcionário antes de excluí-lo.")
    try:
        employee.user.delete()
    except ProtectedError:
        # Appointment.employee é PROTECT (histórico de agendamento/comissão
        # não pode sumir junto) — ver apps/scheduling/models.py
        raise ValidationError(
            "Não é possível excluir: existem agendamentos vinculados a este funcionário."
        )


@transaction.atomic
def set_working_hours(employee, entries):
    """Substitui a jornada semanal inteira (RF10).

    `entries`: lista de dicts {weekday, start_time, end_time}.
    """
    for entry in entries:
        if entry["end_time"] <= entry["start_time"]:
            raise ValidationError(
                "O horário de fim deve ser depois do horário de início "
                f"({entry['start_time']:%H:%M}–{entry['end_time']:%H:%M})."
            )
    WorkingHours.objects.filter(employee=employee).delete()
    WorkingHours.objects.bulk_create(
        [
            WorkingHours(
                tenant=employee.tenant,
                employee=employee,
                weekday=entry["weekday"],
                start_time=entry["start_time"],
                end_time=entry["end_time"],
            )
            for entry in entries
        ]
    )
    return employee


def link_service(employee, service, *, commission_type=None, commission_value=None):
    """Vincula funcionário a serviço (RF09), com override opcional de comissão."""
    if service.tenant_id != employee.tenant_id:
        raise ValidationError(
            "Serviço e funcionário precisam pertencer ao mesmo salão."
        )
    has_type = commission_type not in (None, "")
    has_value = commission_value not in (None, "")
    if has_type != has_value:
        raise ValidationError(
            "Override de comissão exige tipo E valor (ou nenhum dos dois)."
        )
    if has_type:
        _validate_commission(commission_type, commission_value)
        commission_value = _to_decimal(commission_value)
    link, _ = EmployeeService.objects.update_or_create(
        employee=employee,
        service=service,
        defaults={
            "tenant": employee.tenant,
            "commission_type": commission_type if has_type else None,
            "commission_value": commission_value if has_value else None,
        },
    )
    return link


def unlink_service(employee, service):
    EmployeeService.objects.filter(employee=employee, service=service).delete()


def get_commission_config(employee, service):
    """Configuração de comissão a usar num atendimento (regra 4 do CLAUDE.md).

    Prioridade: override do vínculo (`EmployeeService`) > padrão do `Employee`.
    A Etapa 7 usa este retorno para gravar o SNAPSHOT na `Commission` — nunca
    recalcular depois com base em valores que podem ter mudado.
    """
    link = EmployeeService.objects.filter(
        employee=employee, service=service
    ).first()
    if link is not None and link.commission_type:
        return link.commission_type, link.commission_value
    return employee.default_commission_type, employee.default_commission_value


@transaction.atomic
def sync_owner_employee(tenant):
    """Cria/atualiza o perfil de funcionário do responsável pelo salão,
    conforme `Tenant.owner_attends` (decisão do usuário em 2026-08-04).

    Reaproveita o `User` role=tenant_admin já existente como `Employee.user`
    — não cria um segundo login. `full_name`/`photo` ficam sincronizados a
    partir de `Tenant.owner_name`/`owner_photo` (fonte da verdade é
    Configurações); jornada, serviços e comissão continuam editados no
    próprio perfil, igual aos demais funcionários. Chamado por
    `apps.tenants.views.settings_view` a cada save de Configurações.
    """
    owner_user = tenant.users.filter(role=User.Role.TENANT_ADMIN).first()
    if owner_user is None:
        return None

    if not tenant.owner_attends:
        Employee.objects.filter(tenant=tenant, user=owner_user).update(is_active=False)
        return None

    employee, created = Employee.objects.get_or_create(
        tenant=tenant,
        user=owner_user,
        defaults={
            "full_name": tenant.owner_name,
            "phone": "",
            "photo": tenant.owner_photo,
            "default_commission_type": CommissionType.PERCENTAGE,
            "default_commission_value": Decimal("0"),
        },
    )
    employee.full_name = tenant.owner_name
    employee.photo = tenant.owner_photo
    employee.is_active = True
    employee.save(update_fields=["full_name", "photo", "is_active"])
    return employee
