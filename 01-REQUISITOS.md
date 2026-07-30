# Requisitos — Zelo

## 1. Visão geral

Plataforma SaaS onde cada **tenant** (salão de estética) tem:
- uma página pública de agendamento (`app.com/<slug>`)
- um painel administrativo (dono/admin do salão)
- um painel restrito por funcionário (agenda própria, comissões)

A plataforma cobra **assinatura mensal dos tenants** via Asaas.

## 2. Personas

| Persona | Acesso | Principais ações |
|---|---|---|
| **Cliente final** | Página pública, sem login | Escolhe serviço → escolhe funcionário → vê agenda → agenda com telefone |
| **Funcionário** | Login (email/senha) | Vê própria agenda, serviços vinculados, comissões por período |
| **Admin do tenant (dono do salão)** | Login (email/senha ou Google) | Gerencia tudo do seu salão: funcionários, serviços, estoque, caixa, configurações |
| **Superadmin da plataforma** | Django Admin / painel próprio | Gerencia tenants, assinaturas, planos, suporte |

## 3. Requisitos funcionais — MVP

### 3.1 Página pública de agendamento
- RF01: Acessível por `app.com/<slug-do-tenant>`, sem necessidade de login.
- RF02: Exibe identidade visual do tenant (logo, foto de capa, imagem de fundo, nome, endereço,
  WhatsApp). A foto de capa aparece no topo do card central, atrás da logo (a logo sobrepõe a
  capa, estilo banner + avatar); sem capa cadastrada, o card volta ao layout simples (só logo).
- RF03: Fluxo de agendamento: **1) escolher serviço → 2) escolher funcionário que presta aquele
  serviço → 3) ver agenda daquele funcionário (respeitando jornada e horários já ocupados) →
  4) escolher horário → 5) informar telefone (+ nome na primeira vez) → 6) confirmar.**
- RF04: Telefone é o identificador único do cliente dentro daquele tenant (não precisa senha).
  Se o telefone já existe, recupera nome e histórico; se não existe, cadastra na hora.
- RF05: Sistema bloqueia horários fora da jornada do funcionário e horários já ocupados.
- RF06: Cliente pode ver/cancelar agendamento futuro reentrando com o telefone.
- RF06b: Atalho de contato via WhatsApp: botão "Fale conosco" na página pública (ícone oficial do
  WhatsApp) abre conversa com o número do tenant; na lista de Clientes do painel, cada cliente com
  telefone válido tem um ícone de WhatsApp (coluna própria) que abre a conversa direto com aquele
  cliente (`wa.me/55<telefone>`). Cliente anonimizado (LGPD) não mostra o ícone.

### 3.1b Login/cadastro do painel (dono do salão)
- RF06c: Login e cadastro do painel (`/painel/login/`, `/cadastrar/`) aceitam **"Continuar com
  Google"** (decisão do usuário em 2026-07-30), além de e-mail/senha. Se o e-mail da conta Google
  já é de um `User` existente, apenas vincula (login); se é novo, cria tenant + `tenant_admin`
  automaticamente (mesmo caminho do cadastro self-service, sem senha — login sempre via Google
  daqui pra frente). Funcionário e cliente final não são afetados: funcionário continua logando
  com a senha que o admin define (RF08), cliente final nunca tem login (regra 6 do CLAUDE.md).

### 3.2 Funcionários
- RF07: Admin cadastra funcionário com: nome, foto, email, telefone, tipo de comissão padrão
  (percentual ou valor fixo) e valor padrão.
- RF08: Ao cadastrar, sistema cria automaticamente um **usuário (email/senha)** para o funcionário
  logar na plataforma daquele tenant.
- RF09: Admin vincula funcionário aos **serviços que ele executa**; esse vínculo pode sobrescrever
  a comissão padrão (comissão específica por serviço).
- RF10: Admin define a **jornada de trabalho** do funcionário (dias da semana + horário de início/fim),
  podendo ter exceções (folgas, bloqueios pontuais).
- RF11: O vínculo funcionário↔serviço reflete automaticamente na página pública.
- RF12: Funcionário logado vê: sua agenda, seus atendimentos por período e o total de comissão
  gerada (pendente/paga) por período.

### 3.3 Serviços
- RF13: Admin cadastra serviço: nome, descrição, duração (minutos), preço, ativo/inativo.
- RF13b: Admin pode excluir um serviço definitivamente (botão + modal de confirmação) — **não**
  precisa mais desativar antes (decisão do usuário em 2026-07-29; o modal é a barreira contra
  clique acidental). Só é bloqueado se existir agendamento vinculado (`Appointment.service` é
  `PROTECT` — histórico nunca é perdido); nesse caso, desativar continua sendo a alternativa. O
  toggle ativo/inativo é verde quando ativo e vermelho quando inativo (mesmo padrão visual em
  Estoque, Funcionários, Planos e Avisos).
- RF14: Serviço só aparece na página pública se tiver ao menos 1 funcionário vinculado e ativo.

### 3.4 Agenda / Agendamentos
- RF15: Agendamento tem status: pendente, confirmado, **em atendimento**, concluído, cancelado,
  não compareceu (no-show). "Em atendimento" é quando o cliente chegou e a comanda abre no Caixa
  (`start_appointment`) — a partir daí não cabe mais cancelar direto (RF06), só remover pela
  comanda (RF17e) ou finalizar (RF16).
- RF16: Ao marcar um agendamento como **concluído**, o sistema:
  - gera a comissão do funcionário (pendente de pagamento);
  - gera lançamento de caixa (entrada);
  - se o atendimento envolveu produto (ex. venda casada), abate estoque e gera a transação
    correspondente.
- RF16b: O crédito do cliente pode ser abatido PARCIALMENTE na comanda (mesmo quando o saldo é
  menor que o total) — o admin digita quanto quer abater (até o saldo disponível) e o restante é
  cobrado por outra forma de pagamento normalmente. Vale tanto pra 1 atendimento quanto pra uma
  comanda com vários serviços (`credit_amount` em `complete_appointment`/`complete_client_comanda`).
- RF17: Admin/funcionário pode criar agendamento manualmente (encaixe, cliente por telefone/balcão).
- RF17b: Com o cliente já no salão, o admin pode adicionar um serviço extra à comanda em
  andamento (ex.: veio pro corte e decidiu fazer manicure) — vira um novo agendamento "em
  atendimento" na hora, sem checar agenda futura, agrupado com os demais atendimentos do mesmo
  cliente no Caixa e fechado num pagamento só (`apps/scheduling/services.py::start_walk_in_service`
  / `complete_client_comanda`). Cada serviço mantém sua própria comissão (podem ser profissionais
  diferentes).
- RF17c: Produto adicionado à comanda fica persistido no banco (`ComandaProductItem`), não em
  memória do navegador — sobrevive a trocar de aba/página antes de finalizar. Um botão só de
  "Vender produto" por comanda (por cliente), não um por serviço/atendimento.
- RF17d: "Nova Venda" no Caixa — venda de produto avulsa, sem nenhum serviço/agendamento
  envolvido (cliente que só entra pra comprar algo). Gera `StockMovement` + `CashTransaction`
  direto, sem `Commission` (`apps/finance/services.py::sell_products`).
- RF17e: O admin pode remover um serviço adicionado por engano na comanda (antes de finalizar) —
  volta pra cancelado, liberando o horário do profissional
  (`apps/scheduling/services.py::remove_appointment_from_comanda`). Se sobrarem produtos
  pendentes sem nenhum serviço na comanda, ela continua aparecendo no Caixa e finaliza como venda
  avulsa (sem comissão).
- RF17f: A aba Comandas do Caixa mostra atendimentos "em atendimento" de **qualquer dia**, não só
  hoje (decisão do usuário em 2026-07-29) — permite antecipar um atendimento (iniciar antes da
  data agendada) e garante que uma comanda esquecida de dias anteriores continue aparecendo pra
  ser finalizada ou corrigida (senão ficava presa "em atendimento" pra sempre, sem UI pra fechar
  ou cancelar). O card mostra um selo com a data quando ela não é hoje (vermelho se no passado,
  laranja se no futuro/antecipado).
- RF17g: A Agenda tem duas visões: **por dia** (padrão, lista vertical) e **semanal** (estilo
  calendário — 7 dias em colunas, horário no eixo vertical, atendimentos posicionados por
  horário/duração e lado a lado quando se sobrepõem). Ambas as visões aceitam filtro por
  funcionário. Clicar num atendimento na visão semanal abre um modal com os detalhes e as mesmas
  ações da visão diária (confirmar, iniciar atendimento, não compareceu, cancelar).

### 3.5 Estoque
- RF18: Cadastro de produto: nome, SKU, unidade, preço de custo, preço de venda, estoque atual,
  estoque mínimo.
- RF19: Toda movimentação de produto (entrada = compra/reposição; saída = venda/uso em serviço/perda)
  gera um `StockMovement`, recalcula o estoque e, quando aplicável, gera transação de caixa.
- RF20: Alerta quando estoque atual ≤ estoque mínimo (visível no painel; base pronta para
  notificação futura por e-mail/WhatsApp).
- RF20b: Admin pode excluir um produto definitivamente (botão + modal de confirmação) — **não**
  precisa mais desativar antes (mesma decisão do RF13b). Só é bloqueado se existir movimentação de
  estoque vinculada (`StockMovement.product` é `PROTECT`); desativar continua sendo a alternativa
  nesse caso.

### 3.6 Caixa / Financeiro
- RF21: Toda transação (venda de serviço, venda de produto, pagamento de comissão, despesa avulsa)
  gera um `CashTransaction` com tipo (entrada/saída), categoria, valor, forma de pagamento, data.
- RF22: Painel de caixa mostra saldo do dia/período, totais por categoria.
- RF23: Admin pode registrar despesas avulsas (aluguel, contas, etc.) manualmente.
- RF24: Admin marca comissões como pagas, o que gera uma saída de caixa vinculada.

### 3.7 Configurações do tenant
- RF25: Upload de imagem de fundo da página pública, logo e foto de capa (RF02).
- RF26: Cadastro de WhatsApp, endereço, nome fantasia, descrição curta, horário de funcionamento
  configurável por dia da semana (aberto/fechado + abertura/fechamento), exibido corretamente na
  página pública (dia atual em destaque, semana completa expansível).
- RF27: Slug customizável (com validação de unicidade) usado na URL pública.

### 3.8 Assinatura SaaS (plataforma → tenant)
- RF28: Ao criar um tenant, gera-se cliente e assinatura no Asaas. **Etapa 9/Asaas foi
  deliberadamente adiada pelo usuário** — hoje `Subscription` nasce automaticamente em
  `register_tenant` (status `trialing`, sem plano), com os campos `asaas_*` reservados pra quando
  a integração automática existir.
- RF29: Webhook do Asaas atualiza status da assinatura (ativa, atrasada, cancelada). *Não
  construído ainda — depende do RF28 ser retomado.*
- RF30: Tenant com assinatura inadimplente perde acesso ao painel admin (a definir regra de
  carência) — página pública pode continuar ativa por X dias (a decidir). *Não construído ainda.*

### 3.9 Painel do superadmin (plataforma) — `/plataforma/`
Painel custom do superadmin, separado do Django Admin cru (que continua em `/superadmin/` — os
dois links coexistem, por instrução explícita do usuário).

- RF31: CRUD de `Plan` (nome, descrição, preço mensal, ativo, ordem) — planos atribuíveis aos
  tenants.
- RF32: Lista de assinantes (todos os tenants, com filtro por status/plano/busca) e ficha do
  assinante — troca manual de plano e status da `Subscription`, suspensão/reativação de acesso
  (`Tenant.is_active`), exclusão definitiva da conta (reusa `delete_tenant_account`, com
  confirmação por digitação do slug).
- RF33: Mini-dashboard da plataforma — total de assinantes ativos/em teste/inadimplentes/
  cancelados, MRR (soma do preço dos planos ativos), novos e cancelados no mês.
- RF34: `superadmin_required` (decorator) restringe todo o painel `/plataforma/` ao
  `role=superadmin`; `painel_home` redireciona superadmin pra lá em vez de cair no painel do
  tenant.

### 3.10 Sistema de notificações da plataforma
- RF35: Superadmin cria avisos (`Announcement`: título, mensagem, ativo/inativo) em
  `/plataforma/avisos/`, broadcast pra **todos** os tenants de uma vez (sem alvo por tenant).
- RF36: Sininho com contador de não-lidos no painel do tenant, visível **só pro `tenant_admin`**
  (não funcionário) — leitura é por usuário (`AnnouncementRead`), cada admin dispensa a sua
  própria, mesmo aviso pode aparecer não-lido pra outro tenant.

## 4. Requisitos funcionais — Fase 2 (planejar modelo de dados agora, não construir agora)

- RF37: Relatórios (faturamento por período, por funcionário, por serviço, produtos mais vendidos,
  DRE simplificado).
- RF38: Notificação automática por WhatsApp (confirmação e lembrete de agendamento) — API oficial
  Meta ou provedor tipo Twilio/Z-API.
- RF39: Notificação de estoque baixo por e-mail/WhatsApp.
- RF40: Avaliação do atendimento pelo cliente (nota + comentário) pós-serviço.
- RF41: Múltiplos planos de assinatura com limites diferentes (nº de funcionários, etc.).
- RF42: App mobile (a API REST via DRF já deve estar pronta para isso).

### 4.1 Estoque profissional (plano iniciado em 2026-07-20 — ver `03-MODELO-DE-DADOS.md` pros modelos)

Ordem de construção seguida (Fornecedor primeiro por ser fundação leve pros dois seguintes;
Validade/Lote antes de Custo médio porque é o risco financeiro mais direto num salão — produto
cosmético vencido é prejuízo na certa; Inventário físico e Curva ABC não têm dependência forte
entre si nem com o resto, ficam por último):

- RF43 ✅ *(implementado)*: Cadastro de fornecedor (nome, contato, telefone, e-mail,
  observações, ativo/inativo — `apps/inventory/models.py::Supplier`). `Product` ganha fornecedor
  preferido (opcional); `StockMovement` de entrada ganha fornecedor da compra específica (pode
  variar por compra). Painel em `/painel/estoque/fornecedores/`.
- RF44 ✅ *(implementado)*: Lote/validade por produto — opt-in por produto
  (`Product.tracks_batches`, nem todo produto precisa, ex. toalha não vence). Cada compra
  (`StockMovement` IN + motivo compra) abre um `ProductBatch` (número do lote, validade,
  quantidade recebida/restante) — exige validade informada. Alerta de lote vencendo em breve
  (`inventory_ops.batches_expiring_soon`, mesma lógica de estoque mínimo, RF20), mostrado no
  card "Lotes Vencendo" da tela de Estoque. Saída desconta do lote mais próximo do vencimento
  primeiro (FEFO) quando o produto rastreia lote, com rastro em `StockMovementBatch` (uma saída
  pode esgotar vários lotes seguidos).
- RF45 ✅ *(implementado)*: Custo médio ponderado automático —
  (`novo_custo = (estoque_atual × custo_atual + qtd_comprada × custo_da_compra) ÷ (estoque_atual + qtd_comprada)`),
  recalculado a cada `StockMovement` de entrada com motivo compra (ajuste/perda não recalculam).
  **Decisão de negócio confirmada com o usuário**: depois da 1ª compra registrada
  (`Product.has_purchase_history`), `Product.cost_price` fica travado — não editável manualmente
  nunca mais (nem no painel, nem via API), só muda via recálculo automático nas próximas compras.
  Antes da 1ª compra, continua editável normalmente (custo inicial estimado no cadastro).
- RF46 ✅ *(implementado)*: Inventário físico (contagem) — `PhysicalInventoryCount` congela o
  estoque esperado (`expected_quantity`) de cada produto ativo no momento em que a contagem
  começa; admin preenche a quantidade contada por produto (pode ser em várias sessões, HTMX
  salva a cada campo); ao fechar a contagem, todo produto com diferença gera um `StockMovement`
  de `adjustment` automático (entrada ou saída conforme o sinal) — itens deixados em branco são
  ignorados (produto não contado nesta rodada). Painel em `/painel/estoque/inventario/`.
- RF47 *(adiado — retomar depois)*: Relatório de giro de estoque e curva ABC — sem model novo,
  só agregação de `StockMovement` num período: giro (saída no período), ranking por valor vendido
  com curva ABC (Pareto: A = 80% acumulado, B = próximos 15%, C = últimos 5%), e visão de
  produtos parados (zero saída no período).
- RF48 *(fora deste plano, só registrado pra não esquecer — pedido explícito do usuário)*: ficha
  técnica por serviço — um serviço (ex. "Coloração") ter uma receita pré-definida de produtos
  consumidos automaticamente, em vez do admin escolher manualmente na hora de fechar a comanda
  (como funciona hoje). Ainda não planejado em detalhe.

## 5. Requisitos não-funcionais

- RNF01: Isolamento de dados garantido entre tenants em **todas** as queries (nunca vazar dado
  de um tenant para outro).
- RNF02: Toda alteração de estoque e caixa deve ser **transacional** (atomicidade — usar
  `transaction.atomic()`), nunca deixar estoque e caixa dessincronizados.
- RNF03: Página pública precisa ser rápida e simples o suficiente para uso em celular (a maioria
  dos clientes finais vai agendar pelo celular).
- RNF04: Senhas com hash (padrão Django), nunca texto puro.
- RNF05: Auditoria mínima: todo registro financeiro/estoque guarda quem criou e quando.
- RNF06: Sistema deve suportar múltiplos tenants na mesma instância sem exigir deploy separado
  por cliente.
- RNF07: LGPD: dado do cliente final (telefone, nome) pertence ao tenant; cliente pode pedir
  exclusão dos seus dados. O admin também pode excluir um cliente pelo painel (botão + modal
  avisando explicitamente o que é apagado) — usa a mesma anonimização (`anonymize_client`): nome e
  telefone somem, mas agendamentos concluídos, comissões, transações de caixa e saldo/histórico da
  carteira de crédito **não são afetados** (preservados para auditoria financeira e relatórios,
  `Appointment.client` é `PROTECT`).

## 6. Fora de escopo (por enquanto)
- Pagamento online do serviço pelo cliente final (só cobrança da assinatura SaaS por ora).
- App mobile nativo.
- Múltiplas unidades/filiais por tenant (assumir 1 tenant = 1 endereço por enquanto).
