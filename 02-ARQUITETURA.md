# Arquitetura — Zellup

## 1. Stack

| Camada | Escolha | Motivo |
|---|---|---|
| Linguagem | Python 3.12 | Definido pelo cliente |
| Framework web | Django 5.x | Admin pronto, ORM maduro, ótimo para domínio CRUD-pesado/financeiro |
| API | Django REST Framework | Expor API para o futuro app mobile e para chamadas HTMX que precisem de JSON |
| Frontend | Django Templates + HTMX + Alpine.js | Um único projeto, um único deploy, menos complexidade operacional |
| Banco de dados | PostgreSQL 16 | Transacional, robusto para dado financeiro, suporta bem multi-tenant row-level |
| Cache / filas | Redis + Celery + Celery Beat | Tarefas assíncronas (webhooks Asaas, jobs agendados, futuros lembretes) |
| Armazenamento de mídia | Volume local no VPS (`django-storages` já configurado, trocável para S3/MinIO depois) | Simplicidade inicial, migração fácil depois |
| Pagamento (assinatura SaaS) | Asaas (API + Webhook) | Definido pelo cliente |
| Servidor de aplicação | Gunicorn | Padrão de mercado para Django |
| Proxy reverso / SSL | Nginx + Certbot | Padrão para VPS próprio |
| Containerização | Docker + Docker Compose | Reprodutibilidade, facilita deploy no VPS |

## 2. Estrutura de apps Django

```
zellup/
├── config/                 # settings, urls raiz, wsgi/asgi, celery.py
├── apps/
│   ├── tenants/             # Tenant, TenantBusinessHours, middleware de resolução de tenant
│   ├── accounts/             # User custom (admin, funcionário, superadmin), auth
│   ├── employees/            # Employee (perfil), WorkingHours/Schedule, EmployeeService
│   ├── services/              # Service
│   ├── scheduling/            # Appointment + regras de disponibilidade + conclusão/comanda
│   ├── clients/                # Client (CRM: mensalista, carteira de crédito)
│   ├── inventory/              # Product, Category, Supplier, StockMovement, ProductBatch,
│   │                           # StockMovementBatch, PhysicalInventoryCount(Item)
│   ├── finance/                  # CashTransaction, Commission, ComandaProductItem
│   ├── billing/                   # Plan, Subscription (manual hoje; Asaas fica reservado)
│   ├── notifications/              # Announcement, AnnouncementRead (avisos da plataforma)
│   ├── dashboard/                   # KPIs agregados do painel do tenant (sem model próprio)
│   └── public/                       # Views/templates da página pública de agendamento
├── templates/
│   └── plataforma/                 # painel custom do superadmin (/plataforma/)
├── static/
├── media/
└── manage.py
```

Regra: **cada app tem uma responsabilidade só**. Regras de negócio ficam em `services.py`
dentro de cada app (não em `views.py` nem em `models.py` além do necessário) — ver `CLAUDE.md`.

## 3. Estratégia multi-tenant

**Modelo escolhido: banco compartilhado, isolamento por `tenant_id` (row-level multi-tenancy).**

Motivos: um único banco Postgres no VPS é mais simples de migrar, fazer backup e operar do
que schema-per-tenant; o volume de dados esperado (salões de estética, não milhares de tenants
gigantes) não justifica a complexidade extra de schemas separados.

Implementação:
- Toda model "tenant-aware" herda de uma `TenantModel` abstrata com `tenant = ForeignKey(Tenant)`.
- Um `TenantManager` customizado facilita `Model.objects.for_tenant(tenant)`.
- **Nunca confiar em filtro manual espalhado pela view.** Usar um middleware que resolve o
  tenant atual (por slug na URL pública, ou por `request.user.tenant` no painel logado) e
  injeta em `request.tenant`; todas as querysets do painel passam por esse contexto.
- Testes de isolamento (dois tenants, garantir que um nunca vê dado do outro) são
  **obrigatórios** antes de qualquer release.

## 4. Roteamento de URLs

```
/                                    → landing da plataforma (institucional/venda do SaaS)
/cadastrar/                          → cadastro self-service do tenant (dono do salão)
/<slug>/                             → página pública de agendamento do tenant
/<slug>/agendar/                     → fluxo de agendamento (HTMX)
/painel/login/                       → login (admin, funcionário OU superadmin — auth única)
/painel/                             → redireciona por role (tenant_admin/employee/superadmin)
/painel/dashboard/                   → KPIs agregados do tenant
/painel/funcionarios/
/painel/servicos/
/painel/agenda/                      → visão por dia (padrão)
/painel/agenda/semana/                → visão semanal (grade estilo calendário)
/painel/clientes/                    → CRM (mensalista, carteira de crédito)
/painel/estoque/                    → produtos, categorias
/painel/estoque/fornecedores/       → cadastro de fornecedor (RF43)
/painel/estoque/<id>/lotes/         → lotes/validade de um produto (RF44)
/painel/estoque/inventario/         → contagens de inventário físico (RF46)
/painel/caixa/                       → Comandas / Comissões / Lançamentos
/painel/avisos/                      → sininho de avisos da plataforma (só tenant_admin)
/painel/configuracoes/
/painel/minha-agenda/ , /minha-comissao/  → views exclusivas do funcionário
/plataforma/                         → painel custom do superadmin (planos, assinantes, avisos)
/api/v1/...                          → DRF, para uso futuro (app mobile)
/webhooks/asaas/                     → webhook de assinatura — não construído ainda (Etapa 9 adiada)
/superadmin/                         → Django Admin cru — mantido separado do /plataforma/ por
                                        instrução explícita do usuário (os dois links coexistem)
```

## 5. Autenticação e permissões

- `User` customizado (`AbstractUser`) com campo `role` (`superadmin`, `tenant_admin`, `employee`)
  e `tenant` (FK, null para superadmin).
- Um `Employee` sempre tem um `User` 1:1 associado (criado automaticamente no cadastro).
- Permissões por `role` + `tenant_id` — nunca por `is_staff` puro (evitar reuso indevido do
  Django Admin para lógica de negócio do tenant).
- Funcionário só acessa: sua própria agenda, seus próprios serviços/comissões. Não acessa
  caixa geral, estoque ou dados de outros funcionários, a menos que seja também admin.
- `tenant_admin_required` / `employee_required` / `superadmin_required` (decorators em
  `apps/accounts/decorators.py`) são o único caminho de checagem de acesso nas views de painel —
  mesmo shape (`@login_required` + checagem de `role`), nunca checagem ad-hoc numa view solta.
  `superadmin_required` não exige `request.tenant` (superadmin sempre tem `tenant=None`).
- **Login com Google (`django-allauth`, decisão do usuário em 2026-07-30)** — só o provedor
  Google é usado; login por e-mail/senha continua sendo o `LoginView` padrão do Django
  (`config/urls.py`), sem depender do allauth. Toda a regra de negócio fica no adapter
  (`apps/accounts/adapters.py::ZellupSocialAccountAdapter`):
  - `pre_social_login`: se o e-mail da conta Google já é de um `User` existente, vincula
    automaticamente (`sociallogin.connect`) — pula a tela de "esse e-mail já existe" do allauth.
  - `save_user` (só roda quando o e-mail é novo): chama `register_tenant(name=..., email=...,
    password=None)` — mesmo caminho do cadastro self-service (`/cadastrar/`), criando o tenant já
    com jornada padrão e assinatura em teste. `password=None` deixa o usuário com senha
    inutilizável (login só via Google); o nome do tenant nasce como "Salão de {nome do Google}" —
    o dono edita depois em Configurações.
  - **Configuração no Google Cloud Console**: projeto → OAuth consent screen (tipo Externo) →
    Credentials → OAuth Client ID (Web application) → Authorized redirect URI =
    `<domínio>/accounts/google/login/callback/` (path fixo do allauth) → `GOOGLE_OAUTH_CLIENT_ID`
    / `GOOGLE_OAUTH_CLIENT_SECRET` no `.env` (nunca hardcoded — lidos em
    `SOCIALACCOUNT_PROVIDERS["google"]["APP"]` em `config/settings.py`).
  - **Gotcha:** ter 2 backends em `AUTHENTICATION_BACKENDS` (`ModelBackend` +
    `allauth.account.auth_backends.AuthenticationBackend`) quebra qualquer `auth_login(request,
    user)` chamado sem `authenticate()` antes (Django não sabe qual backend usar) — por isso
    `apps/tenants/views.py::signup_view` passa `backend="django.contrib.auth.backends.ModelBackend"`
    explicitamente depois do cadastro self-service.
  - **Gotcha:** `SOCIALACCOUNT_LOGIN_ON_GET = True` é necessário pra pular a tela intermediária
    sem estilo do allauth ("Você está prestes a fazer login...") e ir direto pro Google no clique
    do botão — sem isso, o clique mostra uma página solta em vez de redirecionar.

## 6. Regras de negócio críticas (motor de agendamento e financeiro)

1. **Disponibilidade de horário** = jornada do funcionário (`WorkingHours`) menos horários já
   ocupados por outros agendamentos (status pendente/confirmado/em atendimento) menos exceções
   (folgas). Não considera `TenantBusinessHours` (horário de funcionamento do salão) — esse é só
   informativo, exibido na página pública, não trava agendamento.
2. **Conclusão de atendimento** dispara, dentro de uma única transação atômica:
   - criação da `Commission` (com base no tipo de comissão do vínculo funcionário↔serviço,
     ou o padrão do funcionário se não houver override);
   - criação do `CashTransaction` de entrada (ou só da PARTE não coberta por crédito do cliente,
     se `credit_amount` foi informado — ver RF16b);
   - se envolver produto: `StockMovement` de saída + recalcula estoque + dispara alerta se
     ficar ≤ mínimo.
3. **Nunca alterar estoque sem gerar `StockMovement`** e nunca gerar `CashTransaction`
   financeiro de venda sem estar amarrado a uma origem rastreável (agendamento, venda avulsa,
   ajuste manual).
4. **Comanda é agrupada por CLIENTE, não por agendamento** (`apps/finance/views.py::_comanda_groups`)
   — um cliente pode ter vários serviços "em atendimento" ao mesmo tempo (RF17b), fechados juntos
   num pagamento só (`complete_client_comanda`), cada um mantendo sua própria comissão. O carrinho
   de produto da comanda (`ComandaProductItem`) também é por cliente, não por agendamento — um só
   botão "Vender produto" pra comanda inteira, e persiste no banco (sobrevive a trocar de
   aba/página, diferente do carrinho antigo em `Alpine.store`, removido). `_comanda_groups` **não
   filtra por data** (RF17f) — mostra "em atendimento" de qualquer dia, senão uma comanda
   antecipada ou esquecida de dia anterior não tem UI pra ser fechada/corrigida.
5. **Estoque profissional (RF43-46, `apps/inventory/services.py`)**:
   - Custo médio (RF45) só recalcula em `StockMovement` de entrada com motivo **compra**
     (ajuste/perda nunca mexem em `cost_price`); depois da 1ª compra, `cost_price` fica travado
     pro admin — a trava vive no `services.py` (`update_product` ignora silenciosamente valor
     manual recebido), não só no form, pra proteger painel HTMX e API DRF ao mesmo tempo.
   - Lote/validade (RF44) é opt-in por produto; saída consome por FEFO entre lotes com saldo,
     podendo esgotar vários lotes numa única movimentação (rastro em `StockMovementBatch`).
   - Fechamento de contagem de inventário (RF46) reaproveita `register_stock_movement` pra gerar
     o ajuste — nenhuma lógica de estoque duplicada fora desse ponto único.
   - **Gotcha recorrente:** todo novo model tenant-aware com `on_delete=PROTECT` apontando pra
     algo que `delete_tenant_account` já apaga (`apps/tenants/services.py`) precisa de uma linha
     manual nova ali — Django não resolve PROTECT mesmo quando os dois lados são apagados juntos
     (já aconteceu com `StockMovement`, `ProductBatch` e `PhysicalInventoryCount`).

6. **Padrão de exclusão (Serviços/Estoque, decisão do usuário em 2026-07-29)**: excluir não exige
   mais desativar antes — o modal de confirmação do painel é a barreira contra clique acidental.
   `delete_service`/`delete_product` continuam bloqueando via `ValidationError` quando há
   `PROTECT` real (agendamento ou movimentação de estoque vinculada); nesse caso a view renderiza
   `painel/_modal_error.html` (mensagem amigável, não um 409 cru) sugerindo desativar em vez de
   excluir. Toggle ativo/inativo é sempre verde/vermelho (`#2e7d32`/erro) em todo o painel —
   Serviços, Estoque, Funcionários, Planos (`/plataforma/`) e Avisos.
7. **Agenda semanal** (`apps/scheduling/views.py::agenda_week`/`_week_grid_context`) — grade estilo
   calendário (7 colunas de dia × eixo de horário), posicionamento em pixel (não em blocos fixos de
   30min): `top`/`height` calculados a partir de `start_time`/duração, faixa de horário exibida
   vem de `TenantBusinessHours` (arredondada pra hora cheia, com fallback 08:00–20:00 sem dado).
   Atendimentos sobrepostos (times diferentes de funcionário no mesmo horário, visão "Todos") são
   distribuídos lado a lado por `_layout_day_events` (cluster + first-fit, mesmo algoritmo usado por
   calendários desse estilo) — dividir em colunas por CLUSTER de sobreposição, não pelo dia
   inteiro, senão eventos sem conflito depois de um cluster ficam espremidos à toa. Ações de
   status (confirmar/iniciar/no-show/cancelar) são as MESMAS views da visão diária — decidem pra
   onde devolver o HTMX (`#agenda-items` vs `#agenda-week-grid`) pelo parâmetro `view=week` na
   query string, não duplicam lógica de negócio.
   - **Gotcha:** valores numéricos usados em CSS inline (`top: {{ }}px`, `calc({{ }}% ...)`) têm
     que ser formatados como string com ponto decimal no Python (`_px()`) antes de ir pro template
     — o Django localiza float automaticamente pra vírgula (`LANGUAGE_CODE=pt-br`), o que gera CSS
     inválido tipo `top: 96,0px` (silenciosamente ignorado pelo navegador, todo posicionamento
     some). Mesmo cuidado que `{% load l10n %}`/`|unlocalize` já resolve em `_comandas.html`.

## 7. Integração Asaas (billing da plataforma) — **adiada, controle manual por enquanto**

Decisão do usuário: a Etapa 9 (integração de verdade com o Asaas) foi deliberadamente adiada.
Enquanto isso, `apps/billing` (`Plan`, `Subscription`) já existe e é gerenciado **manualmente**
pelo superadmin em `/plataforma/` — troca de plano/status na mão, sem nenhuma chamada de API real.
Os campos abaixo já estão reservados no model `Subscription` pra quando isso for retomado:

- Ao criar tenant → cria `customer` no Asaas → cria `subscription` (cobrança recorrente).
- Endpoint `webhooks/asaas/` recebe eventos (`PAYMENT_CONFIRMED`, `PAYMENT_OVERDUE`, etc.),
  processado via Celery task (idempotente, validando assinatura do webhook).
- Regra de bloqueio de acesso por inadimplência: **a decidir com o cliente** (ex: X dias de
  carência) — deixar isso como flag configurável (`grace_period_days`, já existe no model) e não
  hardcoded.

## 8. Segurança

- HTTPS obrigatório (Certbot).
- CSRF ativo em todos os formulários (padrão Django).
- Rate limiting no endpoint de agendamento público (evitar spam/flood de agendamentos falsos)
  — `django-ratelimit` ou similar, por IP + telefone.
- Validação de telefone (formato BR) no back e no front.
- Segredos (Asaas API key, `SECRET_KEY`, credenciais de banco) sempre via variáveis de
  ambiente (`.env`), nunca commitados.

## 9. Observabilidade (mínimo viável)
- Logs estruturados (Django logging + arquivo rotativo no VPS).
- Sentry (ou similar) para captura de erros em produção — recomendado, mas pode entrar depois
  do MVP funcional.
