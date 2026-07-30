# Modelo de Dados — Zelo

Convenção: todo model marcado com 🔒 herda de `TenantModel` (tem `tenant_id` obrigatório e
passa pelo isolamento multi-tenant). Todo model financeiro/estoque guarda `created_by` e
`created_at`.

## Diagrama (visão geral)

```mermaid
erDiagram
    TENANT ||--o{ USER : possui
    TENANT ||--o{ EMPLOYEE : possui
    TENANT ||--o{ SERVICE : oferece
    TENANT ||--o{ CLIENT : atende
    TENANT ||--o{ PRODUCT : estoca
    TENANT ||--o{ APPOINTMENT : agenda
    TENANT ||--o{ CASH_TRANSACTION : registra
    TENANT ||--|| SUBSCRIPTION : assina
    TENANT ||--o{ TENANT_BUSINESS_HOURS : "tem horário (7 dias)"

    PLAN ||--o{ SUBSCRIPTION : "é o plano de"
    USER ||--o{ ANNOUNCEMENT_READ : dispensa

    USER ||--o| EMPLOYEE : "é (opcional)"
    EMPLOYEE ||--o{ WORKING_HOURS : tem
    EMPLOYEE ||--o{ EMPLOYEE_SERVICE : executa
    SERVICE ||--o{ EMPLOYEE_SERVICE : "é executado por"

    CLIENT ||--o{ APPOINTMENT : agenda
    CLIENT ||--o{ CLIENT_CREDIT_TRANSACTION : "tem carteira"
    CLIENT ||--o{ COMANDA_PRODUCT_ITEM : "tem carrinho pendente"
    EMPLOYEE ||--o{ APPOINTMENT : atende
    SERVICE ||--o{ APPOINTMENT : "é o serviço de"

    APPOINTMENT ||--o| COMMISSION : gera
    APPOINTMENT ||--o{ CASH_TRANSACTION : gera
    APPOINTMENT ||--o{ STOCK_MOVEMENT : "pode gerar"

    CATEGORY ||--o{ PRODUCT : agrupa
    SUPPLIER ||--o{ PRODUCT : "é preferido de"
    SUPPLIER ||--o{ STOCK_MOVEMENT : "forneceu (compra)"
    SUPPLIER ||--o{ PRODUCT_BATCH : "forneceu (lote)"
    PRODUCT ||--o{ STOCK_MOVEMENT : movimenta
    PRODUCT ||--o{ COMANDA_PRODUCT_ITEM : "pendente em comanda"
    PRODUCT ||--o{ PRODUCT_BATCH : "tem lote (opt-in)"
    PRODUCT ||--o{ PHYSICAL_INVENTORY_COUNT_ITEM : "é contado em"
    STOCK_MOVEMENT ||--o| CASH_TRANSACTION : "pode gerar"
    STOCK_MOVEMENT ||--o{ STOCK_MOVEMENT_BATCH : "consome de (FEFO)"
    PRODUCT_BATCH ||--o{ STOCK_MOVEMENT_BATCH : "é consumido em"
    PHYSICAL_INVENTORY_COUNT ||--o{ PHYSICAL_INVENTORY_COUNT_ITEM : contém
    COMMISSION ||--o| CASH_TRANSACTION : "gera ao pagar"
```

## Entidades

### `Tenant`
| Campo | Tipo | Obs |
|---|---|---|
| id | UUID | PK |
| name | string | Nome fantasia do salão |
| slug | string, único | Usado na URL pública `/​<slug>/` |
| whatsapp | string | |
| address | string | |
| description | text | opcional |
| logo | image | |
| cover_image | image | capa exibida no topo do card da página pública, atrás da logo |
| background_image | image | fundo da página pública |
| is_active | bool | desativado manualmente pelo superadmin |
| created_at | datetime | |

### `TenantBusinessHours` 🔒 (horário de funcionamento do salão)
Substituiu o antigo campo livre `business_hours_note` — 1 linha por dia da semana, sempre as 7
(criadas automaticamente em `register_tenant`). Puramente informativo (não trava agendamento —
ver [[WorkingHours]] do funcionário pra isso).

| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| weekday | int (0=Seg..6=Dom) | mesma convenção de `WorkingHours.weekday` |
| is_closed | bool | quando true, `start_time`/`end_time` ficam `null` |
| start_time | time, null | |
| end_time | time, null | |
| _constraint_ | unique(tenant, weekday) | |

### `Plan`
Plano de assinatura da plataforma — editável pelo superadmin em `/plataforma/planos/`.

| Campo | Tipo | Obs |
|---|---|---|
| name | string, único | |
| description | string | |
| price | decimal | preço mensal |
| is_active | bool | some da lista de planos atribuíveis quando desativado |
| order | int | ordem de exibição |
| created_at | datetime | |

### `Subscription` (billing / Asaas)
Nasce automaticamente em `register_tenant` (status `trialing`, sem `plan`) — controle
**manual** por enquanto (Etapa 9/Asaas adiada pelo usuário); os campos `asaas_*` já ficam
reservados pra quando a integração automática existir.

| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant (1:1) | |
| plan | FK Plan, PROTECT, null=True | atribuído manualmente pelo superadmin |
| asaas_customer_id | string | reservado (Etapa 9) |
| asaas_subscription_id | string | reservado (Etapa 9) |
| status | enum: trialing, active, overdue, canceled | mudado manualmente pelo superadmin |
| grace_period_days | int | tolerância antes de bloquear acesso |
| current_period_start | date, null | reinicia (= hoje) a cada troca de plano; editável manualmente pelo superadmin quando não há cobrança recorrente (`Subscription.is_recurring`) |
| current_period_end | date, null | `current_period_start` + 30 dias corridos a cada troca de plano |
| created_at | datetime | |
| updated_at | datetime | |

### `Announcement`
Aviso de atualização do app, criado pelo superadmin em `/plataforma/avisos/` — broadcast pra
**todos** os tenants (sem alvo por tenant). Visível só a `tenant_admin` (sininho no painel).

| Campo | Tipo | Obs |
|---|---|---|
| title | string | |
| message | text | |
| is_active | bool | avisos antigos podem ser desativados |
| created_by | FK User, SET_NULL | |
| created_at | datetime | |

### `AnnouncementRead`
Leitura é por **usuário** (não por tenant) — cada `tenant_admin` dispensa a sua própria.

| Campo | Tipo | Obs |
|---|---|---|
| announcement | FK Announcement, CASCADE | |
| user | FK User, CASCADE | |
| read_at | datetime | |
| _constraint_ | unique(announcement, user) | |

### `User` (custom, `AbstractUser`)
| Campo | Tipo | Obs |
|---|---|---|
| id | UUID | |
| email | string, único | usado como login |
| tenant | FK Tenant, null=True | null apenas para superadmin |
| role | enum: superadmin, tenant_admin, employee | |
| is_active | bool | |

### `Employee` 🔒
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| user | FK User (1:1) | criado automaticamente no cadastro |
| full_name | string | |
| photo | image | perfil público |
| bio | text | opcional, aparece na página pública |
| phone | string | |
| default_commission_type | enum: percentage, fixed | |
| default_commission_value | decimal | percentual (0-100) ou valor R$, conforme tipo |
| is_active | bool | |

### `WorkingHours` 🔒 (jornada do funcionário)
| Campo | Tipo | Obs |
|---|---|---|
| employee | FK Employee | |
| weekday | int (0=Seg..6=Dom) | |
| start_time | time | |
| end_time | time | |
| is_active | bool | permite desativar um dia sem apagar |

### `ScheduleException` 🔒 (folgas/bloqueios pontuais — opcional no MVP, mas já modelar)
| Campo | Tipo | Obs |
|---|---|---|
| employee | FK Employee | |
| date | date | |
| start_time / end_time | time, null=True | null = dia inteiro bloqueado |
| reason | string | opcional |

### `Service` 🔒
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| name | string | |
| description | text | |
| duration_minutes | int | usado para calcular slots de agenda |
| price | decimal | |
| is_active | bool | |
| — unique_together | (tenant, name) | evita serviço duplicado no mesmo salão |

### `EmployeeService` 🔒 (vínculo funcionário↔serviço)
| Campo | Tipo | Obs |
|---|---|---|
| employee | FK Employee | |
| service | FK Service | |
| commission_type | enum: percentage, fixed, null | null = usa o padrão do funcionário |
| commission_value | decimal, null | override específico deste serviço |
| — unique_together | (employee, service) | |

### `Client` 🔒 (cliente final, identificado por telefone — CRM)
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| phone | string | identificador dentro do tenant — propriedade `whatsapp_url` (`wa.me/55<phone>`) retorna `None` quando o telefone foi anonimizado (LGPD, formato `removido-{pk}`) |
| name | string | |
| preferences | text | texto livre (alergia, produto preferido, observações) |
| is_subscriber | bool | mensalista |
| subscription_due_date | date, null | vencimento MENSAL (não data de início) — propriedades `subscription_is_overdue`/`subscription_is_due_soon` |
| credit_balance | decimal | **derivado**, nunca editado direto — só via `ClientCreditTransaction`/serviços de `apps/clients/services.py` |
| created_at | datetime | |
| — unique_together | (tenant, phone) | mesmo telefone pode existir em tenants diferentes |

### `ClientCreditTransaction` 🔒 (ledger da carteira de crédito do cliente)
Recarregar gera `CashTransaction` real (categoria `client_credit_topup`) — o dinheiro entrou de
verdade. Usar o crédito depois (comanda) só abate saldo, NÃO gera nova `CashTransaction` (evita
contar receita duas vezes — decisão do usuário).

| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| client | FK Client, PROTECT | |
| type | enum: in, out | reaproveita `CashFlowType` |
| amount | decimal | |
| reason | string | "Recarga", "Uso em atendimento", "Ajuste manual", "Estorno" |
| related_appointment | FK Appointment, PROTECT, null | setado quando o crédito foi usado pra pagar uma comanda |
| related_cash_transaction | FK CashTransaction, PROTECT, null | setado quando a recarga gerou entrada real de caixa |
| created_by | FK User, null | |
| created_at | datetime | |

### `Appointment` 🔒
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| client | FK Client | |
| employee | FK Employee | |
| service | FK Service | |
| date | date | |
| start_time / end_time | time | end_time calculado por `service.duration_minutes` |
| status | enum: pending, confirmed, in_progress, completed, canceled, no_show | `in_progress` = "Em Atendimento" (cliente chegou, comanda aberta no Caixa) |
| price_at_booking | decimal | snapshot do preço no momento (preço pode mudar depois) |
| notes | text | opcional |
| created_at | datetime | |
| created_by | FK User, null | null se criado pelo cliente na página pública |

### `Category` 🔒 (categoria de produto)
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| name | string | |
| — unique_together | (tenant, name) | |

Exclusão bloqueada (`PROTECT` em `Product.category`) enquanto houver produto vinculado —
reatribuir os produtos antes de excluir a categoria.

### `Product` 🔒
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| name | string | |
| sku | string | |
| category | FK Category, PROTECT, null=True | opcional no model, obrigatório no form do painel |
| supplier | FK Supplier, SET_NULL, null=True | fornecedor preferido (opcional, RF43) |
| unit | enum: un, ml, l, g, kg, cx, par | `WHOLE_UNIT_CODES = {un, par, cx}` só aceitam quantidade inteira (validado em qualquer venda) |
| cost_price | decimal | **custo médio (RF45)** — trava (não editável manualmente) assim que `has_purchase_history` é True; só muda via recálculo automático numa compra |
| sale_price | decimal | |
| current_stock | decimal | **derivado**, sempre recalculado via `StockMovement` |
| min_stock_alert | decimal | |
| tracks_batches | bool, default False | opt-in (RF44) — nem todo produto vence (ex. toalha) |
| is_active | bool | |

### `StockMovement` 🔒
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| product | FK Product, PROTECT | |
| type | enum: in, out | entrada/saída |
| quantity | decimal | |
| unit_price | decimal | preço no momento do movimento |
| total_value | decimal | `quantity * unit_price` |
| reason | enum: purchase, sale, service_use, adjustment, loss | |
| supplier | FK Supplier, SET_NULL, null=True | fornecedor **desta compra específica** (RF43) — pode diferir do preferido em `Product.supplier` |
| related_appointment | FK Appointment, null | |
| created_by | FK User | |
| created_at | datetime | |

### `Supplier` 🔒 (fornecedor, RF43)
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| name | string | |
| contact_name | string, blank | |
| phone | string, blank | |
| email | string, blank | |
| notes | text, blank | |
| is_active | bool | |
| — unique_together | (tenant, name) | |

### `ProductBatch` 🔒 (lote/validade, RF44)
Aberto a cada `StockMovement` de compra (`type=in, reason=purchase`) quando
`Product.tracks_batches` é True — exige `expiry_date`. Saída desconta por FEFO (lote mais
próximo do vencimento primeiro) entre os lotes com saldo.

| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| product | FK Product, PROTECT | |
| batch_number | string, blank | número/código do lote (livre) |
| expiry_date | date | |
| quantity_received | decimal | snapshot da quantidade recebida nessa compra |
| quantity_remaining | decimal | **derivado**, abatido a cada saída que consumir deste lote |
| supplier | FK Supplier, SET_NULL, null | |
| unit_cost | decimal | custo da compra que abriu o lote |
| received_at | datetime | |

### `StockMovementBatch` 🔒 (rastro de consumo de lote, RF44)
Uma saída pode esgotar um lote e continuar consumindo do próximo (FEFO) — por isso é uma tabela
à parte e não uma FK direta em `StockMovement`.

| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| movement | FK StockMovement, CASCADE | |
| batch | FK ProductBatch, PROTECT | |
| quantity | decimal | quantidade consumida deste lote por este movimento |

### `PhysicalInventoryCount` 🔒 (contagem de inventário físico, RF46)
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| status | enum: in_progress, completed | fechada = não editável mais |
| started_at | datetime | |
| completed_at | datetime, null | |
| created_by | FK User, null | |
| notes | text, blank | |

### `PhysicalInventoryCountItem` 🔒 (RF46)
1 linha por produto ativo, congelada no início da contagem.

| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| count | FK PhysicalInventoryCount, CASCADE | |
| product | FK Product, PROTECT | |
| expected_quantity | decimal | snapshot de `Product.current_stock` no início da contagem |
| counted_quantity | decimal, null | preenchido pelo admin (pode ficar em branco até contar) |
| — constraint | unique(count, product) | |

Ao fechar a contagem: todo item com `counted_quantity` preenchido e diferente de
`expected_quantity` gera um `StockMovement` automático (`reason=adjustment`, `type=in` se
sobrou, `type=out` se faltou) — sem model novo pra isso, reusa `register_stock_movement`. Itens
deixados em branco (produto não contado nesta rodada) são ignorados.

### `CashTransaction` 🔒 (caixa)
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| type | enum: in, out | entrada/saída |
| category | enum: service_sale, product_sale, commission_payment, client_credit_topup, expense, other | `client_credit_topup` = recarga de crédito do cliente (entrada real de caixa) |
| amount | decimal | |
| payment_method | enum: cash, pix, credit_card, debit_card, client_credit, other | `client_credit` NUNCA aparece aqui de fato — `_validate_cash_transaction` rejeita explicitamente (pagar com crédito não gera `CashTransaction`, ver `Client.credit_balance` abaixo); o valor existe no enum só por uso interno/histórico |
| description | string | |
| related_appointment | FK Appointment, null | |
| related_stock_movement | FK StockMovement, null | |
| related_commission | FK Commission, null | |
| created_by | FK User | |
| created_at | datetime | |

### `Commission` 🔒
| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| employee | FK Employee | |
| appointment | FK Appointment (1:1) | |
| commission_type | enum: percentage, fixed | snapshot do tipo usado no cálculo |
| commission_value | decimal | snapshot do valor/percentual usado |
| base_amount | decimal | valor do serviço sobre o qual incidiu |
| calculated_amount | decimal | valor final da comissão |
| status | enum: pending, paid | |
| paid_at | datetime, null | |

### `ComandaProductItem` 🔒 (carrinho de produto pendente da comanda)
Produto adicionado a uma comanda em andamento, ainda NÃO vendido de verdade — sem
`StockMovement`/`CashTransaction` ainda. Existir como registro no banco (não só em memória no
navegador) é o que garante que sobrevive a trocar de aba/página antes de fechar a conta. Um botão
só de "Vender produto" por comanda (por CLIENTE, não por serviço/atendimento) — vira venda real
(e é apagado) só quando `complete_client_comanda` finaliza a comanda.

| Campo | Tipo | Obs |
|---|---|---|
| tenant | FK Tenant | |
| client | FK Client | |
| product | FK Product, PROTECT | |
| quantity | decimal | |
| created_by | FK User, null | |
| created_at | datetime | |
| _constraint_ | unique(client, product) | clicar de novo no mesmo produto não duplica a linha |

## Entidades planejadas — Estoque profissional (ver `01-REQUISITOS.md` §4.1)

`Supplier`, `ProductBatch`, `StockMovementBatch`, `PhysicalInventoryCount` e
`PhysicalInventoryCountItem` (RF43-46) já foram implementadas — ver as entidades acima, na seção
principal. Fica pendente só o RF47 (relatório de giro/curva ABC — sem model novo, adiado pra
depois) e, fora deste plano, a ficha técnica por serviço (RF48).

## Regras de cálculo (documentar no código também, não só aqui)

- `Commission.calculated_amount`:
  - se `commission_type == percentage`: `base_amount * (commission_value / 100)`
  - se `commission_type == fixed`: `commission_value`
- Prioridade de comissão: `EmployeeService.commission_value` (se definido) > `Employee.default_commission_value`.
- `Product.current_stock` nunca é editado diretamente — sempre resultado de soma/subtração de
  `StockMovement` (garantir isso via `service` de domínio, não permitir edição direta no admin
  de estoque atual).
- Pagamento de comanda com crédito do cliente pode ser PARCIAL: `credit_amount` abate primeiro do
  serviço, depois de cada produto na ordem informada, até esgotar — o restante de cada categoria
  vira `CashTransaction` normal. Ver RF16b em `01-REQUISITOS.md`.
- *(planejado, RF45)* Custo médio ponderado: `novo_custo = (estoque_atual × custo_atual +
  qtd_comprada × custo_da_compra) ÷ (estoque_atual + qtd_comprada)`, recalculado a cada
  `StockMovement` de entrada.
