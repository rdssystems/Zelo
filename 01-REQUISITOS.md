# Requisitos — Zellup

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
- RF06: Cliente pode ver/cancelar agendamento futuro reentrando com o telefone. Cada agendamento
  mostra um selo de status ✅ *(2026-07-31)*: "Aguardando confirmação" (pendente), "Confirmado" ou
  "Em atendimento" — reflete direto o que o admin do salão faz na Agenda (RF15b).
- RF06i ✅ *(implementado em 2026-07-31)*: Tela de sucesso do agendamento
  (`/<slug>/agendar/sucesso/<id>/`) reflete o `status` real do agendamento, em vez de sempre
  dizer "Confirmado" — controlado por `Tenant.auto_confirm_appointments` (checkbox em
  Configurações, RF26d, default desmarcado): **desmarcado** → agendamento nasce `pending`, a
  tela mostra "Agendamento Enviado" + aviso pra acompanhar em "Meus agendamentos" até o salão
  confirmar na Agenda (mesmo fluxo de sempre, RF15b); **marcado** → agendamento já nasce
  `confirmed` (`apps.scheduling.services.create_appointment`), a tela mostra "Agendamento
  Confirmado" na hora, sem esperar o salão.
- RF06b: Atalho de contato via WhatsApp: botão "Fale conosco" na página pública (ícone oficial do
  WhatsApp) abre conversa com o número do tenant; na lista de Clientes do painel, cada cliente com
  telefone válido tem um ícone de WhatsApp (coluna própria) que abre a conversa direto com aquele
  cliente (`wa.me/55<telefone>`). Cliente anonimizado (LGPD) não mostra o ícone.
- RF06d ✅ *(implementado em 2026-07-31)*: Campanha de cobrança de mensalista por WhatsApp —
  botão "Cobrar mensalistas" (ao lado de "Novo Cliente" em `/painel/clientes/`) abre modal com
  selectbox **Vencidas / A Vencer** (janela configurável, ver RF26b abaixo); lista o mensalista
  daquela situação com telefone + mensagem pronta (editável), e "Enviar e ir pro próximo" abre o
  WhatsApp (`wa.me/55<telefone>?text=<mensagem>`) um cliente por vez, num loop dentro do próprio
  modal (Alpine.js, sem round-trip ao servidor por envio). Só entram clientes com telefone válido
  (mesmo critério do RF06b — anonimizado LGPD fica de fora).
- RF06e ✅ *(implementado em 2026-07-31)*: Cliente sem atendimento concluído há
  `Tenant.client_inactive_days` dias (RF26b) aparece com um badge "Inativo" ao lado do nome na
  lista de Clientes — conta a partir do cadastro (`created_at`) se o cliente nunca voltou.
- RF06f ✅ *(implementado em 2026-07-31)*: Cancelamento pelo cliente avisa o salão. Ao confirmar
  "Cancelar agendamento" em `/meus-agendamentos/`, se `Tenant.whatsapp_cancel_redirect_enabled`
  estiver ligado (default `True`, toggle em Configurações — RF26c) e o salão tiver WhatsApp
  cadastrado, o mesmo clique abre `wa.me/<número do salão>?text=<mensagem>` numa aba nova com um
  aviso pronto ("Sou {cliente} e decidi cancelar meu agendamento de {serviço} marcado para
  {data/hora}. Só avisando por aqui!") — o cliente só edita se quiser e clica enviar dentro do
  próprio WhatsApp. **Independente desse toggle**, todo cancelamento pelo cliente também: (1)
  grava `Appointment.canceled_by_client=True`, mostrando "Cancelado pelo cliente" (com ícone) em
  vez de só "Cancelado" na Agenda (lista do dia, grade semanal e modal de detalhe); (2) gera uma
  `TenantNotification` (RF06g) — decisão do usuário: a notificação interna não pode depender do
  cliente efetivamente mandar a mensagem no WhatsApp (ele pode fechar a aba sem enviar), então é
  sempre criada, o WhatsApp é só um reforço a mais.
- RF06g ✅ *(implementado em 2026-07-31)*: Notificações operacionais do salão. Novo tipo de
  alerta, `apps.notifications.models.TenantNotification` — diferente de `Announcement` (aviso da
  plataforma pra **todos** os tenants), é escopado a **um** tenant, nascendo de eventos dentro do
  próprio salão (hoje só "cliente cancelou agendamento", RF06f; o `kind` já é um enum extensível
  pra outros eventos futuros, ver roadmap "tempo real" abaixo). Aparece em três lugares, ao mesmo
  tempo (decisão do usuário — "sininho + aviso no canto + no card"):
  1. **Sininho** (ícone já existente na barra lateral, renomeado de "Novidades" pra
     "Notificações") — o contador soma avisos da plataforma (`Announcement`) +
     `TenantNotification` não lidas deste tenant; o modal ao clicar mostra as duas listas em
     seções separadas ("Agenda" e "Novidades da plataforma"), cada uma com "marcar como
     lida"/"marcar todas como lidas".
  2. **Aviso no canto da tela ("toast")** — `painel/base.html` faz polling
     (`hx-trigger="every 20s"`) em `notifications:agenda_toast_poll`, que devolve só as
     notificações novas desde a última checada nesta sessão (watermark por `pk` guardado em
     `request.session`, não por `is_read` — o toast aparece uma vez por notificação por sessão de
     navegador, independente de o admin marcar como lida ou não) e as injeta via HTMX OOB em
     `#toast-slot`; cada toast some sozinho depois de 8s (ou ao clicar o X).
  3. **Card do agendamento na Agenda** — RF06f acima.
- RF06h ✅ *(implementado em 2026-07-31, base pro RF06f/g)*: Agenda com atualização automática —
  `#agenda-items` (lista do dia) e `#agenda-week-grid` (grade semanal) fazem polling
  (`hx-trigger="every 20s"`) contra endpoints dedicados (`scheduling:agenda_items_poll`/
  `agenda_week_poll`) que devolvem só o partial, sem fechar modal aberto no meio de uma ação —
  pensado pro PC do salão que fica com a Agenda aberta o dia inteiro (pedido do usuário).
  **Roadmap explicitamente não implementado agora** (pedido do usuário, "colocar nos planos"):
  notificar automaticamente TODA mudança relevante na agenda — novo agendamento, reagendamento,
  etc. — não só cancelamento pelo cliente. Polling a cada 20s já cobre a necessidade prática de
  hoje (a Agenda se atualiza sozinha, o sininho/toast avisam de cancelamento) com zero
  infraestrutura nova. Pra virar "tempo real" de verdade (latência sub-segundo, sem esperar o
  próximo ciclo de poll) o próximo passo natural seria WebSockets (Django Channels) ou
  Server-Sent Events com Redis como backend de pub/sub — ambos exigem processo assíncrono
  adicional rodando ao lado do `gunicorn` atual (`docker-compose.yml` ganharia mais um serviço) e
  não são gratuitos em complexidade operacional; recomendação: só migrar pra isso se o polling de
  20s se mostrar insuficiente na prática (esse número é fácil de ajustar pra baixo primeiro).

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
- RF12b ✅ *(implementado em 2026-08-07)*: Autonomia do funcionário na própria agenda —
  3 toggles independentes em Configurações (`Tenant.employee_can_create_appointments`,
  `employee_can_confirm_appointments`, `employee_can_start_appointments`; todos desligados por
  padrão, decisão do usuário). Ligados, o funcionário ganha em "Minha Agenda" (`/painel/minha-agenda/`)
  os mesmos botões de agendar/confirmar/iniciar atendimento que o admin tem em `/painel/agenda/`
  (`apps.accounts.decorators.scheduling_action_required`) — **sempre restrito ao PRÓPRIO
  agendamento** (nunca de um colega): "agendar" só permite escolher a si mesmo como profissional
  (`NewAppointmentForm(lock_employee=...)`, queryset travado — rejeita `employee` forjado no
  POST) e confirmar/iniciar validam dono do agendamento
  (`apps.scheduling.views._employee_actor_mismatch`) antes de agir. Finalizar a comanda
  (RF16/RF17) continua exclusivo do admin no Caixa — fora do escopo desses 3 toggles. Admin nunca
  é afetado por essas flags (acesso completo sempre, como já era).

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
- RF15b ✅ *(implementado em 2026-07-31)*: Confirmar um agendamento pendente (`/painel/agenda/`,
  lista do dia ou grade semanal) abre um modal em vez de confirmar na hora — mostra uma mensagem
  de confirmação pra WhatsApp pronta (editável) com nome do cliente, serviço, data/horário e
  profissional. O botão "Confirmar e avisar no WhatsApp" faz as duas coisas juntas: confirma de
  verdade (`pending` → `confirmed`) e abre `wa.me/55<telefone>?text=<mensagem>` numa aba nova
  pro admin só clicar enviar. Cliente sem telefone válido (anonimizado LGPD) só vê o botão
  "Confirmar", sem a parte de WhatsApp.
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
  Funcionário só a partir de 2026-08-07 e só com a permissão ligada em Configurações — ver RF12b.
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
- RF26b ✅ *(implementado em 2026-07-31)*: Card "Mensalistas e engajamento" em Configurações com
  dois campos numéricos: dias de antecedência pra avisar mensalidade a vencer
  (`Tenant.subscription_due_soon_days`, default 7 — usado no badge "Vence em breve" da lista de
  Clientes e na campanha de WhatsApp, RF06d) e dias sem atendimento pra marcar cliente inativo
  (`Tenant.client_inactive_days`, default 60 — RF06e).
- RF26c ✅ *(implementado em 2026-07-31)*: Checkbox "Ao cliente cancelar... abrir o WhatsApp do
  salão..." junto do campo WhatsApp em Configurações — controla
  `Tenant.whatsapp_cancel_redirect_enabled` (default `True`), usado pelo RF06f. Não afeta a
  notificação interna (RF06g), que sempre acontece.
- RF26d ✅ *(implementado em 2026-07-31)*: Card "Confirmação de agendamento" em Configurações —
  checkbox "Confirmar agendamento automaticamente" controla
  `Tenant.auto_confirm_appointments` (default desmarcado/`False`, RF06i).
- RF26e ✅ *(implementado em 2026-08-01)*: **Tema visual por tenant** (salão de beleza ou
  barbearia) — `Tenant.theme` (`TenantTheme`, default `salao`). Escolhido no cadastro
  (`/cadastrar/`, dois cards clicáveis "Salão de Beleza"/"Barbearia", sem JS — `input radio`
  escondido + `label` estilizado via `peer-checked`) e editável depois em Configurações (mesmo
  componente, dentro do card "Identidade visual"). **Só muda aparência** (paleta de cores,
  tipografia, raio de borda de cards/modais) — nenhuma regra de negócio ou funcionalidade
  muda entre os dois temas, decisão confirmada com o usuário. Afeta tanto a página pública
  quanto o painel administrativo inteiro; não afeta login/cadastro/páginas legais (antes de
  logar não existe um tenant resolvido ainda) nem `/plataforma/` (ferramenta do superadmin, não
  é de um tenant específico). Paleta "Barbearia" ("Heritage & Steel") validada antes no Google
  Stitch — ver `design-reference/barbearia/`. Cadastro via Google (sem formulário) sempre nasce
  `salao`; o dono troca depois se quiser, decisão deliberada pra não adicionar uma tela extra
  nesse fluxo.
  **Formato dos botões ✅ *(implementado em 2026-08-01)*:** novo token `borderRadius.pill` em
  `templates/_theme_tailwind_config.html` — `9999px` (pílula, igual hoje) no tema salão, `0.5rem`
  (8px, "Tailored Square") no tema barbearia. 155 ocorrências de `rounded-full` em 63 templates
  trocadas pra `rounded-pill` (botões de ação com texto — "Confirmar", "Salvar", "Criar meu
  salão" etc. — e selos de status como "Ativo"/"Confirmado"/"Vencida"); as outras 85 ocorrências
  (fotos de perfil, avatares, botões de ícone único, círculos decorativos) continuam
  `rounded-full` propositalmente, nos dois temas — são círculo de verdade, não escolha de marca.

### 3.8 Assinatura SaaS (plataforma → tenant)
- RF28: Ao criar um tenant, gera-se cliente e assinatura no Asaas. **Etapa 9/Asaas foi
  deliberadamente adiada pelo usuário** — hoje `Subscription` nasce automaticamente em
  `register_tenant` (status `trialing`, sem plano), com os campos `asaas_*` reservados pra quando
  a integração automática existir.
- RF29: Webhook do Asaas atualiza status da assinatura (ativa, atrasada, cancelada). *Não
  construído ainda — depende do RF28 ser retomado.*
- RF30 ✅ *(implementado em 2026-07-31, ver detalhe em §4.2)*: Tenant com assinatura
  inadimplente perde acesso ao painel admin, respeitando `grace_period_days` de carência —
  página pública continua ativa normalmente, sem limite de dias. **Extensão ✅ (mesmo dia):** o
  mesmo bloqueio vale pro trial gratuito de 7 dias vencido sem plano escolhido — nesse caso,
  sem tolerância extra (bloqueia assim que `trial_ends_at` passa).

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

- RF37 ✅ *(implementado em 2026-08-05)*: Relatórios (faturamento por período, por funcionário,
  por serviço, produtos mais vendidos, DRE simplificado) — `/painel/relatorios/`
  (`apps/reports/`), período escolhido pelo usuário (reaproveita
  `apps.finance.services.period_summary` pro DRE). ⚠️ **Pendente**: usuário apontou que ter
  `/painel/dashboard/` (janela fixa hoje/mês) e `/painel/relatorios/` (período livre) como duas
  telas separadas parece redundante — sugestão do agente foi fundir em uma página só (abas
  "Visão Geral" + "Relatórios"), mas a decisão **ainda não foi tomada**, sessão seguiu pra outros
  assuntos antes de fechar isso. Retomar antes de considerar RF37 totalmente encerrado.
- RF38: Notificação automática por WhatsApp (confirmação e lembrete de agendamento) — API oficial
  Meta ou provedor tipo Twilio/Z-API.
- RF39: Notificação de estoque baixo por e-mail/WhatsApp.
- RF40: Avaliação do atendimento pelo cliente (nota + comentário) pós-serviço.
- RF41: Múltiplos planos de assinatura com limites diferentes (nº de funcionários, etc.) — ver
  detalhamento e proposta em `4.2`.
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

### 4.2 Planos, trial e checkout self-service (RF41 + parte do RF28/29 — implementado em
2026-07-30, ver `03-MODELO-DE-DADOS.md`)

**Trial ✅ *(implementado; reduzido de 14 para 7 dias em 2026-08-06, decisão do usuário — só vale
pra cadastro novo, assinatura já existente continua contando os dias que já tinha)*:**
`register_tenant` agora seta `Subscription.trial_ends_at` = 7 dias corridos a partir do cadastro
(`apps.billing.services.TRIAL_DAYS`), acesso completo até lá, sem cartão de crédito. Exibido no
painel (`/painel/plano/`) com contagem regressiva.

**Plano + dias restantes no menu lateral ✅ *(implementado em 2026-07-31)*:** embaixo do
e-mail e do nome do salão, em todo o painel (não só em `/painel/plano/`), o tenant_admin vê o
plano atual e a contagem regressiva —
`apps.billing.context_processors.sidebar_plan` (registrado em `TEMPLATES` em
`config/settings.py`) + `templates/painel/_sidebar_plan.html`. Só conta como "plano pago" com
`status == active` (webhook confirmou o 1º pagamento no Asaas); escolher um plano e não pagar
deixa `status == pending` com `subscription.plan` preenchido, mas o menu continua mostrando
"Gratuito" — decisão do usuário em 2026-07-31 ("não pagou, é gratuito"), corrigindo um bug do
corte anterior que mostrava o nome do plano pago assim que escolhido, antes da confirmação.
"Gratuito · N dias restantes" conta `trial_ends_at` por data de calendário (no dia do cadastro
mostra 7, não 6, desde a redução do trial em 2026-08-06); com plano ativo mostra o nome do plano
e os dias restantes do período atual
(`current_period_end`). Só aparece pro tenant_admin (mesmo critério do sininho de avisos), não
pra funcionário. Migration de dados `billing/migrations/0006_reset_free_trial_to_14_days.py`
reiniciou, uma única vez, o `trial_ends_at` de toda assinatura gratuita existente para 14 dias
a partir de 2026-07-31.

**Bug corrigido em 2026-07-31 — "Meu Plano" não mostrava dias restantes com assinatura
`pending`:** `my_plan` (`apps/billing/views.py`) só calculava `days_left` com
`status == trialing`; uma assinatura `pending` (plano escolhido, pagamento não confirmado) não
mostrava nada, mesmo contando como gratuita agora (ver item acima). Trocado pra
`status != active`, e o banner de `pending` em `painel/billing/my_plan.html` passou a exibir a
contagem também. Achado no mesmo bug: a assinatura de teste "Catherine's Sthetic" (pending,
plano Ilimitado) tinha `trial_ends_at` nulo — nunca tinha passado pelo trial — corrigido por
`billing/migrations/0007_backfill_missing_trial_ends_at.py` (preenche 14 dias a partir de hoje
em qualquer assinatura não-ativa sem `trial_ends_at`).

**3 planos seedados ✅ *(implementado, `billing/migrations/0005_seed_plans.py`)*:**

| | Individual | Profissional | Studio |
|---|---|---|---|
| Preço | R$ 69,90/mês *(atualizado em 2026-08-05)* | R$ 129,90/mês *(atualizado em 2026-08-05)* | R$ 179,90/mês |
| Funcionários (marketing) | 0 (só o dono) | até 3 | até 6 |
| Estoque (marketing) | básico | profissional completo (RF43-46) | profissional completo |
| Extras (marketing) | — | comissão automática, CRM completo, pacotes de mensalidade, relatórios (em breve) | idem Profissional *(único diferencial é o limite de funcionários, decisão de 2026-08-05)* |

⚠️ *(texto de marketing acima escrito antes do enforcement existir — ver correção abaixo, RF41
"Pendente")* O texto é copy de vitrine (`apps/billing/views.py::PLAN_HIGHLIGHTS`); o enforcement
de verdade é código, ver abaixo.

**Checkout self-service dentro do painel ✅ *(implementado)*:** `/painel/plano/` — tenant_admin
logado escolhe um plano (`billing:select_plan`), o sistema pede CPF/CNPJ do salão se ainda não
tiver (`Tenant.document`, validado com dígito verificador real —
`apps.tenants.models.validate_cpf_cnpj`), cria cliente + assinatura no Asaas
(`apps/billing/asaas_client.py`) e embute a fatura (`invoiceUrl`) num iframe dentro do próprio
painel (`billingType=UNDEFINED` — cliente escolhe PIX/boleto/cartão na própria fatura), com link
"abrir em nova aba" como alternativa. Status novo `SubscriptionStatus.PENDING` cobre o intervalo
entre "escolheu o plano" e "webhook confirmou o pagamento" — antes só existia `trialing` pra
esse limbo. Polling HTMX (`billing:checkout_status`, a cada 4s) atualiza a tela sozinha quando o
pagamento é confirmado.

**Webhook do Asaas ✅ *(implementado, `POST /webhooks/asaas/`)*:** valida o header
`asaas-access-token` contra `ASAAS_WEBHOOK_TOKEN` (`.env`); idempotente via
`AsaasWebhookEvent` (`unique_together` em `payment_id`+`event_type` — Asaas reenvia o mesmo
evento se não recebe 200 a tempo, reenvio é ignorado sem reprocessar). `PAYMENT_CONFIRMED`/
`PAYMENT_RECEIVED` → `active`; `PAYMENT_OVERDUE` → `overdue`; `PAYMENT_DELETED`/
`PAYMENT_REFUNDED`/`SUBSCRIPTION_DELETED` → `canceled`.

**Estado real das credenciais**: `ASAAS_API_KEY`/`ASAAS_WEBHOOK_TOKEN` seguem vazios no `.env`
(decisão do usuário — só preencher depois). Com a chave vazia, `asaas_client` levanta
`AsaasNotConfigured` e a tela de checkout mostra aviso amigável ("pagamento ainda não ativado")
em vez de quebrar — todo o resto (models, views, templates, testes) já está pronto, só falta a
chave de sandbox/produção pra virar cobrança de verdade.

**RF30 (bloqueio por inadimplência) ✅ *(implementado em 2026-07-31)*:**
`apps.billing.services.subscription_blocks_panel_access(tenant)` decide se o painel bloqueia —
`canceled` bloqueia na hora; `overdue` só bloqueia depois de estourado o `grace_period_days`
(default 5) contado do fim do último período pago (`current_period_end`); sem
`current_period_end` conhecido bloqueia direto (mais seguro que deixar passar sem limite);
`pending`/`active` nunca bloqueiam. Decisão do usuário: bloqueia o painel inteiro
(admin **e** funcionário), com uma única exceção — as telas de `apps.billing` que o admin
precisa pra regularizar (`/painel/plano/`, seleção de plano, checkout, envio de CPF/CNPJ,
polling de status). Página pública de agendamento (`/<slug>/`) nunca é afetada.

**Extensão ✅ *(mesmo dia, mesma função)*: bloqueio do trial vencido (14 dias na época, 7 dias
desde 2026-08-06).** Até então
`trialing` nunca bloqueava — o gap era real: nenhum job muda o `status` quando `trial_ends_at`
passa (não existe Celery beat configurado no projeto), então o tenant ficava com acesso
irrestrito pra sempre depois do trial. Agora `subscription_blocks_panel_access` também bloqueia
quando `status == trialing` e `trial_ends_at` (datetime) já passou — decisão do usuário: **sem
tolerância extra** (a contagem regressiva já fica visível o tempo todo no menu lateral/Meu
Plano, então o vencimento não é surpresa; `grace_period_days` continua exclusivo de assinatura
paga vencida, não é reaproveitado aqui). Sem `trial_ends_at` setado (não deveria acontecer —
`create_subscription_for_tenant` sempre seta — mas por segurança) não bloqueia. Continua
computado a cada request, nenhum campo novo de banco nem job agendado foram necessários. O
banner de "Meu Plano" (`painel/billing/my_plan.html`) e o rótulo "Gratuito" do menu lateral
(`apps.billing.context_processors.sidebar_plan`) mostram "expirado"/aviso de bloqueio nesse
estado, em vez da contagem regressiva.

Aplicado nos dois decorators de painel (`apps/accounts/decorators.py`):
`tenant_admin_required(allow_when_blocked=True)` isenta uma view específica (só usado nas 5
views de billing citadas acima); qualquer outra view do tenant_admin redireciona pra
`/painel/plano/` com uma mensagem. `employee_required` não tem pra onde redirecionar (funcionário
não acessa `/painel/plano/`), então devolve 403 direto com mensagem pra falar com o admin.
`templates/painel/billing/my_plan.html` mostra contagem regressiva da carência (dias restantes
até bloquear) ou aviso de já bloqueado, conforme o caso.

**`Plan.max_employees` ✅ *(implementado em 2026-08-04, `billing/migrations/0008`)*:** campo real
em `Plan`, seedado (Individual=1, Profissional=3, Studio=6 — Individual era 0 até 2026-08-07, ver
abaixo). Enforcement em `apps.billing.services.assert_can_add_employee` — bloqueia
criar/reativar funcionário quando `funcionários_ativos >= plano.max_employees`
(`employee_seats_used`); `max_employees=None` continua sem limite (reservado pra plano
customizado); sem plano (trial) não há limite.

**Trava de downgrade ✅ *(implementado em 2026-08-07)*:** o enforcement acima só pegava na hora de
*adicionar* funcionário — um tenant podia trocar pra um plano menor e ficar "acima do limite" sem
aviso nenhum, só esbarrando por acaso na próxima contratação. `assert_plan_fits_employee_count`
(mesmo arquivo) agora barra a troca de plano ANTES de ir pro checkout quando
`funcionários_ativos > novo_plano.max_employees` — usado tanto em `select_plan` (self-service,
`/painel/plano/`) quanto em `change_subscription_plan` (override do superadmin em
`/plataforma/assinantes/`). Mensagem explica quantos funcionários o tenant tem e o limite do
plano; não desativa ninguém automaticamente — o admin decide quem desativar (em Funcionários) ou
desiste da troca e continua no plano atual (que fica intocado, a exceção interrompe antes do
`save`).

**Dono ("também atende") passa a ocupar vaga ✅ *(implementado em 2026-08-07 — reverte decisão de
2026-08-04)*:** decisão original era o perfil do responsável (RF: "também atende", reaproveita o
próprio login de admin) NUNCA contar pro limite do plano — só "conta de login" contava. Usuário
percebeu a brecha: o dono gera comissão e atende cliente igual um funcionário contratado (CPF
dele ≠ CNPJ do salão, é gente trabalhando de verdade), então um Profissional (3) com o dono
atendendo + 3 contratados eram 4 pessoas atendendo pelo preço de 3. Agora `employee_seats_used`
conta o `Employee` do dono igual qualquer outro quando `is_active=True` (só o LOGIN continua
sendo o mesmo `User` do admin, isso não muda). Efeitos:
- `Plan.max_employees` do Individual subiu de 0 pra 1 (`billing/migrations/0012`) — sem isso, o
  próprio dono não conseguiria ligar "também atende" no plano feito exatamente pra esse caso.
  Profissional (3) e Studio (6) não mudaram de número nem de preço — já eram vendidos como
  "pessoas atendendo no total", só a aplicação estava incompleta.
- Ligar "também atende" em Configurações agora também respeita a vaga
  (`apps.employees.services.sync_owner_employee` chama `assert_can_add_employee` ANTES de
  criar/reativar o perfil do dono — nunca depois, senão a própria vaga nova se conta a mais no
  total). Desligar continua sempre livre (devolve a vaga).
- Se ligar o toggle estoura a vaga, `apps.tenants.views.settings_view` reverte
  `Tenant.owner_attends` pra `False` (senão o Tenant ficaria com o campo `True` sem o `Employee`
  correspondente ativo, já que `form.save()` persiste o formulário inteiro antes de
  `sync_owner_employee` checar a vaga) e mostra a mensagem de erro — não desativa ninguém
  automaticamente, mesma filosofia da trava de downgrade acima.

**Ainda pendente pra fechar RF41 de vez:** `Plan.stock_professional_enabled` como campo real e o
enforcement de esconder telas de estoque profissional (RF43-46) pra quem não tem no plano —
provavelmente em `apps/tenants/services.py` ou um middleware novo, decisão de onde colocar ainda
em aberto.

### 4.3 Roadmap de profissionalização do painel (combinado com o usuário em 2026-08-05)

Ordem de execução acordada com o usuário — cada item só começa depois do anterior estar
concluído e validado ("um a um, sob orientação do usuário"); não é ranking de prioridade de
negócio, é o ritmo de execução escolhido. Retomar por aqui na próxima sessão sobre este assunto.

1. ✅ **Backup offsite** *(concluído 2026-08-05)* — Restic + Cloudflare R2, rodando 2x/dia (3h e
   14h), retenção 7 diários + 4 semanais + 6 mensais, restore já validado com checksum batendo
   100%, alerta de falha no Discord distinguindo estágio local vs offsite (testado com falha
   simulada de verdade). Detalhe técnico completo em `VPS-INFRAESTRUTURA-ATUAL.md` §3.5 (não
   duplicar aqui, aquele arquivo é a fonte da verdade de infraestrutura).
2. **Monitoramento** *(ainda não iniciado — usuário pulou pro item 3 antes)* — 3 camadas
   discutidas: (a) uptime externo (UptimeRobot batendo em `/healthz/` a cada poucos minutos →
   Discord), (b) erro de aplicação (Sentry, com integração nativa de alerta no Discord), (c)
   eventos operacionais direto do código (mesmo padrão de webhook Discord já validado no item 1).
   Decisão de qual camada atacar primeiro ainda em aberto.
3. **RF37 — Relatórios** ✅ *(implementado em 2026-08-05, com ressalva)* — ver linha 283 (o
   detalhe e a pendência de fundir com o Dashboard estão lá, não duplicado aqui).
   - **Extra não planejado, feito no mesmo dia**: compressão de imagem no upload (logo, capa,
     fundo, foto de funcionário/responsável) — não fazia parte do roadmap, usuário pediu ao notar
     que nenhum upload tinha limite de tamanho. `apps/utils.py::compress_uploaded_image`,
     redimensiona a até 1600px preservando formato/transparência, ligado no `save()` de `Tenant`
     e `Employee`.
4. **RF40** — Avaliação do atendimento pelo cliente (nota + comentário pós-serviço) — ver linha
   288.
5. **RF42** — App mobile *(último da lista, de propósito)* — a API REST via DRF já é construída
   desde o início do projeto pensando nisso (regra do `CLAUDE.md`), então o gap aqui é o app em
   si, não a base técnica.

Origem da lista: levantamento de gaps numa sessão de 2026-08-05, comparando o Zellup com o que
sistemas de salão concorrentes costumam oferecer, cruzado com pendências já registradas em
`VPS-INFRAESTRUTURA-ATUAL.md` (backup externo, Sentry) — inclui também a discussão paralela sobre
split de pagamento via Asaas (fora desta lista — depende de CNPJ, registrado só na conversa, não
em requisito formal ainda).

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
