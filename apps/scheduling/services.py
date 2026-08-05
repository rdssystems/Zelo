"""Operações de domínio de agendamento (RF03, RF05, RF06, RF15, RF16).

A conclusão de atendimento (comissão + caixa + estoque, atômica) orquestra
apps/finance e apps/inventory — ver `complete_appointment`.
"""

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.finance.models import CashCategory, CashFlowType
from apps.finance.services import create_cash_transaction, create_commission_for_appointment
from apps.inventory.models import WHOLE_UNIT_CODES, MovementReason, MovementType, Product
from apps.inventory.services import register_stock_movement
from apps.notifications.models import TenantNotificationKind
from apps.notifications.services import create_tenant_notification

from .availability import compute_end_time, is_slot_available
from .models import Appointment, AppointmentStatus, BLOCKING_STATUSES


def _covering_package(client, service):
    """Pacote de mensalidade vigente do cliente que inclui este serviço, ou
    `None` (decisão do usuário em 2026-08-04) — usado tanto no agendamento
    normal quanto no serviço avulso adicionado direto na comanda
    (`start_walk_in_service`)."""
    if client.is_subscriber and client.package_id and client.package.services.filter(pk=service.pk).exists():
        return client.package
    return None


@transaction.atomic
def create_appointment(
    *, tenant, client, employee, service, date, start_time, created_by=None, notes=""
):
    """Cria o agendamento como `pending` (RF15), ou já `confirmed` se o
    salão ligou `Tenant.auto_confirm_appointments` em Configurações
    (decisão do usuário em 2026-07-31 — pula a etapa manual de confirmar na
    Agenda). Revalida a disponibilidade no momento da confirmação (RF05) —
    a tela de horários pode estar desatualizada entre a consulta e o clique
    em confirmar."""
    if not (
        client.tenant_id == tenant.id
        and employee.tenant_id == tenant.id
        and service.tenant_id == tenant.id
    ):
        raise ValidationError("Dados de tenants inconsistentes.")
    if not employee.is_active:
        raise ValidationError("Este profissional não está mais disponível.")
    if not service.is_active:
        raise ValidationError("Este serviço não está mais disponível.")
    if not is_slot_available(employee, service, date, start_time):
        raise ValidationError(
            "Esse horário não está mais disponível. Escolha outro horário."
        )

    end_time = compute_end_time(start_time, service.duration_minutes)
    initial_status = (
        AppointmentStatus.CONFIRMED
        if tenant.auto_confirm_appointments
        else AppointmentStatus.PENDING
    )
    # Snapshot do pacote de mensalidade vigente (decisão do usuário em
    # 2026-08-04) — `price_at_booking` continua o valor de TABELA do
    # serviço mesmo coberto por pacote (base de comissão); é este campo que
    # depois faz `complete_appointment` não cobrar o cliente de novo.
    package = _covering_package(client, service)
    try:
        appointment = Appointment.objects.create(
            tenant=tenant,
            client=client,
            employee=employee,
            service=service,
            date=date,
            start_time=start_time,
            end_time=end_time,
            status=initial_status,
            price_at_booking=service.price,
            package=package,
            notes=notes,
            created_by=created_by,
        )
    except IntegrityError:
        # unique_active_appointment_per_slot pegou uma corrida: outra pessoa
        # confirmou esse mesmo horário entre a checagem acima e este insert.
        raise ValidationError(
            "Esse horário acabou de ser reservado por outra pessoa. Escolha outro horário."
        )

    # `created_by=None` é como `apps.public.views` sinaliza que foi o
    # cliente quem marcou (ver 03-MODELO-DE-DADOS.md) — mesmo critério que
    # `canceled_by_client` usa pra decidir se gera notificação (decisão do
    # usuário em 2026-08-06, estendendo RF06g pra também cobrir agendamento
    # novo, não só cancelamento).
    if created_by is None:
        create_tenant_notification(
            tenant,
            kind=TenantNotificationKind.APPOINTMENT_CREATED_BY_CLIENT,
            title="Novo agendamento! 📅",
            message=(
                f"{client.name} agendou {service.name} para o dia "
                f"{date.strftime('%d/%m/%Y')} às {start_time.strftime('%H:%M')} "
                f"com {employee.full_name}."
            ),
            appointment=appointment,
        )
    return appointment


@transaction.atomic
def start_walk_in_service(*, tenant, client, employee, service, created_by):
    """Serviço extra vendido na hora, com o cliente já no salão (ex.: veio
    pro corte e decidiu fazer manicure também) — cria um novo `Appointment`
    já "em atendimento", direto, SEM passar pela checagem de disponibilidade
    futura de `is_slot_available` (é agora; o admin sabe quem está livre).
    Aparece imediatamente agrupado com os outros atendimentos do cliente na
    aba Comandas do Caixa (mesmo client, mesmo dia)."""
    if not (
        client.tenant_id == tenant.id
        and employee.tenant_id == tenant.id
        and service.tenant_id == tenant.id
    ):
        raise ValidationError("Dados de tenants inconsistentes.")
    if not employee.is_active:
        raise ValidationError("Este profissional não está mais disponível.")
    if not service.is_active:
        raise ValidationError("Este serviço não está mais disponível.")

    now = timezone.localtime()
    start_time = now.time()
    end_time = compute_end_time(start_time, service.duration_minutes)
    package = _covering_package(client, service)
    try:
        return Appointment.objects.create(
            tenant=tenant,
            client=client,
            employee=employee,
            service=service,
            date=now.date(),
            start_time=start_time,
            end_time=end_time,
            status=AppointmentStatus.IN_PROGRESS,
            price_at_booking=service.price,
            package=package,
            created_by=created_by,
        )
    except IntegrityError:
        # unique_active_appointment_per_slot: esse profissional já tem outro
        # atendimento em andamento neste exato minuto (corrida rara de dois
        # cliques quase simultâneos).
        raise ValidationError(
            "Este profissional já tem outro atendimento em andamento agora."
        )


def remove_appointment_from_comanda(appointment):
    """Remove um atendimento "Em Atendimento" da comanda por engano (ex.: o
    admin adicionou o serviço errado via `start_walk_in_service`, ou iniciou
    o atendimento errado) — volta pra cancelado, liberando o horário do
    profissional de novo. Diferente de `cancel_appointment` (que só age em
    pendente/confirmado, pensado pro cliente cancelar antes de chegar): esta
    é a correção do ADMIN depois que o atendimento já começou, direto pelo
    Caixa. Uma vez concluído (`complete_appointment`), não cabe mais remover
    assim — a correção vira estorno manual."""
    if appointment.status != AppointmentStatus.IN_PROGRESS:
        raise ValidationError("Só é possível remover atendimentos em andamento.")
    appointment.status = AppointmentStatus.CANCELED
    appointment.save(update_fields=["status"])
    return appointment


_CANCELABLE_STATUSES = [AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED]


def cancel_appointment(appointment, *, canceled_by_client=False):
    """RF06: cliente pode cancelar um agendamento futuro (pendente/confirmado).
    Uma vez "em atendimento" (o cliente já chegou), não cabe mais cancelar —
    só finalizar a comanda pelo Caixa.

    `canceled_by_client=True` (chamado só por `apps.public.views`) grava a
    origem no próprio agendamento (badge "Cancelado pelo cliente" na Agenda)
    e sempre gera uma `TenantNotification` — decisão do usuário em
    2026-07-31: a notificação interna independe de o salão ter o
    redirecionamento por WhatsApp ligado ou não (esse é só um empurrãozinho
    a mais pro cliente avisar; a notificação é a rede de segurança)."""
    if appointment.status not in _CANCELABLE_STATUSES:
        raise ValidationError("Este agendamento não pode mais ser cancelado.")
    appointment.status = AppointmentStatus.CANCELED
    update_fields = ["status"]
    if canceled_by_client:
        appointment.canceled_by_client = True
        update_fields.append("canceled_by_client")
    appointment.save(update_fields=update_fields)
    if canceled_by_client:
        create_tenant_notification(
            appointment.tenant,
            kind=TenantNotificationKind.APPOINTMENT_CANCELED_BY_CLIENT,
            title="Cliente cancelou um agendamento",
            message=(
                f"{appointment.client.name} cancelou {appointment.service.name} do dia "
                f"{appointment.date.strftime('%d/%m/%Y')} às "
                f"{appointment.start_time.strftime('%H:%M')} com {appointment.employee.full_name}."
            ),
            appointment=appointment,
        )
    return appointment


def confirm_appointment(appointment):
    """RF15: pendente → confirmado."""
    if appointment.status != AppointmentStatus.PENDING:
        raise ValidationError("Só é possível confirmar agendamentos pendentes.")
    appointment.status = AppointmentStatus.CONFIRMED
    appointment.save(update_fields=["status"])
    return appointment


def start_appointment(appointment):
    """Cliente chegou e o atendimento começou: pendente/confirmado → em
    atendimento. A partir daqui, a comanda aparece no Caixa (aba Comandas)
    pra venda de produto e finalização — a Agenda não conclui mais direto."""
    if appointment.status not in _CANCELABLE_STATUSES:
        raise ValidationError(
            "Só é possível iniciar atendimento de agendamentos pendentes ou confirmados."
        )
    appointment.status = AppointmentStatus.IN_PROGRESS
    appointment.save(update_fields=["status"])
    return appointment


def mark_no_show(appointment):
    """RF15: cliente não compareceu."""
    if appointment.status not in _CANCELABLE_STATUSES:
        raise ValidationError(
            "Só é possível marcar não comparecimento em agendamentos pendentes ou confirmados."
        )
    appointment.status = AppointmentStatus.NO_SHOW
    appointment.save(update_fields=["status"])
    return appointment


@transaction.atomic
def complete_appointment(*, appointment, payment_method, created_by, product_usage=None, credit_amount=None):
    """RF16 / regra 3 do CLAUDE.md — operação central do sistema. Concluir um
    atendimento é atômico (tudo ou nada):
    1. status → completed;
    2. `Commission` (snapshot da regra de prioridade EmployeeService > Employee);
    3. `CashTransaction` de entrada pela venda do serviço (ou pela parte dela
       não coberta por crédito, ver abaixo);
    4. se envolveu produto (venda casada): `StockMovement` de saída +
       `CashTransaction` de entrada correspondente à venda do produto (idem).

    Crédito do cliente (RF do "abater ou não" — pedido do usuário quando o
    saldo é insuficiente pro total): `credit_amount` é opcional, quanto do
    total (serviço + produtos) deve ser abatido do saldo do cliente; o
    RESTANTE é cobrado via `payment_method` (dinheiro real — nunca cria
    `CashTransaction` com `PaymentMethod.CLIENT_CREDIT`). O abatimento é
    aplicado primeiro no serviço, depois em cada produto na ordem informada,
    até esgotar `credit_amount` — cada categoria só gera `CashTransaction`
    pela parte que sobrar depois do crédito.

    Atalho retrocompatível: `payment_method=PaymentMethod.CLIENT_CREDIT` (sem
    informar `credit_amount`) continua significando "100% do total vem do
    crédito" — usado pela API/DRF, que ainda não tem a tela de abatimento
    parcial.

    `product_usage`: lista opcional de dicts
    `{"product": Product, "quantity": Decimal, "unit_price": Decimal}`.
    """
    from apps.finance.models import PaymentMethod

    if appointment.status not in BLOCKING_STATUSES:
        raise ValidationError("Só é possível concluir agendamentos pendentes ou confirmados.")

    used_old_full_credit_shortcut = payment_method == PaymentMethod.CLIENT_CREDIT and credit_amount is None
    credit_amount = Decimal("0") if credit_amount is None else Decimal(credit_amount)
    if credit_amount < 0:
        raise ValidationError({"credit_amount": "O valor de crédito não pode ser negativo."})

    appointment.status = AppointmentStatus.COMPLETED
    appointment.save(update_fields=["status"])

    # Serviço coberto por pacote de mensalidade (decisão do usuário em
    # 2026-08-04): `price_at_booking` continua o valor de tabela (base de
    # comissão quando o pacote permite), mas não é cobrado do cliente de
    # novo — já foi pago na hora que o pacote foi atribuído
    # (`apps.clients.services.assign_package_to_client`). `payable_service_total`
    # é o que efetivamente entra no total a pagar/abater da comanda; o
    # serviço em si nunca gera `CashTransaction` quando coberto.
    is_package_covered = appointment.package_id is not None
    commission = None
    if not is_package_covered or appointment.package.generates_commission:
        commission = create_commission_for_appointment(appointment)

    service_total = appointment.price_at_booking
    payable_service_total = Decimal("0") if is_package_covered else service_total

    total_products = Decimal("0")
    product_lines = []
    for item in product_usage or []:
        product = item["product"]
        quantity = item["quantity"]
        unit_price = item["unit_price"]
        movement = register_stock_movement(
            tenant=appointment.tenant,
            product=product,
            movement_type=MovementType.OUT,
            quantity=quantity,
            unit_price=unit_price,
            reason=MovementReason.SERVICE_USE,
            created_by=created_by,
            related_appointment=appointment,
        )
        item_total = quantity * unit_price
        total_products += item_total
        product_lines.append((product, movement, item_total))

    grand_total = payable_service_total + total_products

    # Atalho antigo (API/DRF): pagar 100% com crédito sem informar valor.
    if used_old_full_credit_shortcut:
        credit_amount = grand_total

    if credit_amount > grand_total:
        raise ValidationError(
            {"credit_amount": "O valor de crédito não pode ser maior que o total da comanda."}
        )

    remaining_credit = credit_amount

    service_credit = min(remaining_credit, payable_service_total)
    service_cash = payable_service_total - service_credit
    remaining_credit -= service_credit
    if service_cash > 0:
        create_cash_transaction(
            tenant=appointment.tenant,
            type=CashFlowType.IN,
            category=CashCategory.SERVICE_SALE,
            amount=service_cash,
            payment_method=payment_method,
            description=f"{appointment.service.name} — {appointment.client.name}",
            created_by=created_by,
            related_appointment=appointment,
        )

    for product, movement, item_total in product_lines:
        item_credit = min(remaining_credit, item_total)
        item_cash = item_total - item_credit
        remaining_credit -= item_credit
        if item_cash > 0:
            create_cash_transaction(
                tenant=appointment.tenant,
                type=CashFlowType.IN,
                category=CashCategory.PRODUCT_SALE,
                amount=item_cash,
                payment_method=payment_method,
                description=f"Venda: {product.name}",
                created_by=created_by,
                related_appointment=appointment,
                related_stock_movement=movement,
            )

    if credit_amount > 0:
        from apps.clients.services import redeem_client_credit_for_appointment

        redeem_client_credit_for_appointment(
            appointment.client,
            amount=credit_amount,
            appointment=appointment,
            created_by=created_by,
        )

    return commission


@transaction.atomic
def complete_client_comanda(
    *, appointments, payment_method, created_by, product_usage_by_appointment=None, credit_amount=None
):
    """Fecha vários atendimentos "em atendimento" do MESMO cliente de uma vez
    só — ex.: cliente fez o corte e resolveu fazer manicure na hora, cada um
    com seu profissional. Um pagamento só (mesma `payment_method` pra tudo),
    mas cada atendimento continua gerando sua PRÓPRIA `Commission` (podem ser
    profissionais diferentes) e suas próprias `CashTransaction`s — só reusa
    `complete_appointment` em loop, dentro de uma única transação atômica
    (tudo ou nada: se um item falhar — ex. crédito do cliente insuficiente
    pra soma total — nenhum dos atendimentos do grupo é concluído).

    `credit_amount`: valor ÚNICO de crédito a abater da comanda INTEIRA (o
    admin digita um valor só pro grupo) — alocado em ordem, primeiro no total
    do primeiro atendimento (serviço + seus produtos), depois no próximo, até
    esgotar, exatamente como `complete_appointment` faz dentro de um mesmo
    atendimento entre serviço e produtos.
    """
    if not appointments:
        raise ValidationError("Nenhum atendimento para finalizar.")
    if len({a.client_id for a in appointments}) > 1:
        raise ValidationError("Todos os atendimentos da comanda devem ser do mesmo cliente.")

    product_usage_by_appointment = product_usage_by_appointment or {}

    if credit_amount is None:
        # Sem valor de crédito explícito pro grupo — comportamento antigo:
        # cada atendimento decide sozinho (o atalho payment_method=client_credit
        # de `complete_appointment` continua valendo atendimento por atendimento).
        return [
            complete_appointment(
                appointment=appointment,
                payment_method=payment_method,
                created_by=created_by,
                product_usage=product_usage_by_appointment.get(appointment.pk, []),
            )
            for appointment in appointments
        ]

    remaining_credit = Decimal(credit_amount)
    if remaining_credit < 0:
        raise ValidationError({"credit_amount": "O valor de crédito não pode ser negativo."})

    commissions = []
    for appointment in appointments:
        product_usage = product_usage_by_appointment.get(appointment.pk, [])
        products_total = sum(
            (item["quantity"] * item["unit_price"] for item in product_usage), Decimal("0")
        )
        # Serviço coberto por pacote não entra no total a pagar/abater
        # (mesma lógica de `complete_appointment` — já foi pago na hora que
        # o pacote foi atribuído ao cliente).
        payable_service_total = (
            Decimal("0") if appointment.package_id is not None else appointment.price_at_booking
        )
        appointment_total = payable_service_total + products_total
        credit_for_this = min(remaining_credit, appointment_total)
        remaining_credit -= credit_for_this
        commissions.append(
            complete_appointment(
                appointment=appointment,
                payment_method=payment_method,
                created_by=created_by,
                product_usage=product_usage,
                credit_amount=credit_for_this,
            )
        )

    if remaining_credit > 0:
        raise ValidationError(
            {"credit_amount": "O valor de crédito informado é maior que o total da comanda."}
        )
    return commissions


def _parse_quantity(raw):
    raw = str(raw).strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValidationError("Quantidade inválida.")


def build_product_usage(*, tenant, product_ids, quantities):
    """Monta a lista de produtos vendidos numa conclusão de atendimento (RF16)
    a partir de listas paralelas id-de-produto/quantidade (uma linha por
    produto escolhido no fechamento da comanda).

    O preço unitário SEMPRE vem do cadastro do produto (`Product.sale_price`)
    — nunca é digitado na hora do fechamento, pra não haver divergência com o
    valor cadastrado em Estoque."""
    if len(product_ids) != len(quantities):
        raise ValidationError("Dados de produtos inconsistentes.")
    usage = []
    seen_ids = set()
    for product_id, raw_quantity in zip(product_ids, quantities):
        product_id = str(product_id).strip()
        if not product_id:
            continue
        if product_id in seen_ids:
            raise ValidationError("O mesmo produto foi adicionado mais de uma vez na lista.")
        seen_ids.add(product_id)
        try:
            product = Product.objects.for_tenant(tenant).get(pk=product_id, is_active=True)
        except Product.DoesNotExist:
            raise ValidationError("Produto inválido.")
        quantity = _parse_quantity(raw_quantity)
        if quantity <= 0:
            raise ValidationError("A quantidade deve ser maior que zero.")
        if product.unit in WHOLE_UNIT_CODES and quantity != quantity.to_integral_value():
            raise ValidationError(
                f"Quantidade de \"{product.name}\" deve ser um número inteiro "
                f"({product.get_unit_display()} não aceita fração)."
            )
        usage.append(
            {"product": product, "quantity": quantity, "unit_price": product.sale_price}
        )
    return usage


def upcoming_appointments_for_client(client):
    """RF06: agendamentos futuros do cliente (para a tela 'meus agendamentos')."""
    today = timezone.localdate()
    return (
        Appointment.objects.for_tenant(client.tenant)
        .filter(client=client, date__gte=today, status__in=BLOCKING_STATUSES)
        .select_related("employee", "service")
        .order_by("date", "start_time")
    )
