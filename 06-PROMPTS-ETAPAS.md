# Prompts — Etapas 2 a 9 (fatias verticais)

Use estes prompts **na ordem**, um por sessão do Claude Code, só depois que a Etapa 1
(`05-PROMPT-INICIAL.md`) estiver concluída e o teste de isolamento multi-tenant passando.
Cada etapa já indica o `/model` e `/effort` recomendados (detalhes do porquê em `CLAUDE.md`).

Preâmbulo comum a todas as etapas abaixo (cole junto com o prompt específico, ou deixe fixo
se seu Claude Code já carrega `CLAUDE.md` automaticamente):

> Leia `CLAUDE.md`, `01-REQUISITOS.md`, `03-MODELO-DE-DADOS.md` antes de começar. Siga as
> regras de negócio inegociáveis descritas em `CLAUDE.md`. Ao final, rode migrations e testes,
> e me dê um resumo curto antes de eu validar e seguirmos para a próxima etapa.

---

## Etapa 2 — Cadastro de Serviços
**Model: Sonnet 5 · Effort: medium**

Implemente o app `services` completo: model `Service` (conforme `03-MODELO-DE-DADOS.md`),
admin Django, `services.py` com as operações de criar/editar/ativar-desativar, serializer +
viewset DRF, view/template no painel (`/painel/servicos/`) usando HTMX. Inclua testes de
CRUD básico e da regra "serviço só aparece na página pública se ativo e com funcionário
vinculado" (mesmo que o vínculo ainda não exista — deixe o teste already preparado para a
Etapa 3).

---

## Etapa 3 — Funcionários, Jornada e Vínculo com Serviços
**Model: Sonnet 5 · Effort: medium** (suba para **high** se a lógica de jornada/exceções
ficar complexa)

Implemente o app `employees`: models `Employee`, `WorkingHours`, `ScheduleException`,
`EmployeeService` (conforme `03-MODELO-DE-DADOS.md`). Regras importantes:
- Ao criar um `Employee`, criar automaticamente o `User` associado (role=`employee`,
  `tenant` correto) e disparar e-mail com credenciais (pode ser só log/console no MVP).
- `EmployeeService` permite override de comissão por serviço; se não houver override, usa o
  padrão do `Employee` (documentar isso no código, não só no `.md`).
- CRUD completo no painel (`/painel/funcionarios/`), incluindo tela de definir jornada
  semanal e vincular serviços.
Inclua testes: criação de usuário automática, prioridade de comissão (override > padrão).

---

## Etapa 4 — Motor de Disponibilidade de Agenda
**Model: Sonnet 5 · Effort: high**

Implemente `apps/scheduling/availability.py`: função que recebe `employee` + `service` +
intervalo de datas e retorna os horários livres, calculados como jornada
(`WorkingHours`) menos agendamentos existentes (status pending/confirmed) menos exceções
(`ScheduleException`). Esta é a lógica mais sensível do projeto — escreva bastante teste de
borda (jornada com múltiplos dias, agendamento que atravessa o fim do expediente, exceção de
dia inteiro vs. parcial, serviço com duração que não cabe no slot restante). Não implemente
ainda a página pública nem o model `Appointment` completo se ele não existir — crie o
essencial do model `Appointment` (conforme `03-MODELO-DE-DADOS.md`) como parte desta etapa,
já que a disponibilidade depende dele.

---

## Etapa 5 — Página Pública de Agendamento
**Model: Sonnet 5 · Effort: medium** (use a skill `frontend-design` para o visual)

Implemente o app `public`: fluxo completo em `/​<slug>/agendar/` — escolher serviço → escolher
funcionário (só os vinculados àquele serviço) → ver agenda (usar `availability.py` da Etapa 4)
→ escolher horário → informar telefone (+ nome se for a primeira vez, criando/recuperando
`Client`) → confirmar. Aplique rate limiting no endpoint de criação de agendamento (por IP +
telefone, ver `02-ARQUITETURA.md` seção Segurança). Use a imagem de fundo e o logo do tenant
(campos já existem em `Tenant`, mesmo que o upload em si venha na Etapa 7). Teste: fluxo
completo end-to-end, e que cliente com telefone repetido recupera o cadastro em vez de duplicar.

---

## Etapa 6 — Estoque
**Model: Sonnet 5 · Effort: medium**

Implemente o app `inventory`: models `Product` e `StockMovement`. **Nunca permitir edição
direta de `current_stock`** — toda mudança passa por uma função em `services.py` que cria o
`StockMovement` e recalcula o estoque de forma atômica. Painel de estoque
(`/painel/estoque/`) com listagem, alerta visual quando `current_stock <= min_stock_alert`,
e tela de registrar entrada/saída manual (compra, ajuste, perda). Teste: baixa de estoque
correta, bloqueio de edição direta, alerta de estoque baixo.

---

## Etapa 7 — Caixa e Comissão (conclusão de atendimento)
**Model: Sonnet 5 · Effort: high**

Implemente o app `finance`: models `CashTransaction` e `Commission`. Implemente a operação
central do sistema: **concluir um `Appointment`**, dentro de `transaction.atomic()`, que:
1. cria a `Commission` (snapshot de tipo/valor conforme regra de prioridade
   `EmployeeService` > `Employee`);
2. cria o `CashTransaction` de entrada correspondente ao valor do serviço;
3. se o atendimento envolveu produto, cria o `StockMovement` de saída (reaproveitar service
   da Etapa 6) e o `CashTransaction` relacionado.
Painel de caixa (`/painel/caixa/`) com saldo do período e totais por categoria; tela de
marcar comissão como paga (gera `CashTransaction` de saída vinculada). Teste: a transação
atômica não pode deixar o sistema em estado inconsistente se qualquer etapa falhar (simule
falha proposital e confira rollback).

---

## Etapa 8 — Configurações do Tenant + Login do Funcionário
**Model: Sonnet 5 · Effort: medium**

Duas frentes nesta etapa:
- **Configurações** (`/painel/configuracoes/`): upload de logo e imagem de fundo (Pillow),
  edição de WhatsApp, endereço, descrição, slug (com validação de unicidade).
- **Painel do funcionário**: login (`/painel/login/`) e, para `role=employee`, uma view
  restrita (`/painel/minha-agenda/` e `/painel/minha-comissao/`) mostrando só os próprios
  agendamentos e comissões (pendente/paga) filtráveis por período. Reforce no teste que um
  funcionário nunca consegue ver dado de outro funcionário nem o caixa geral do tenant.

---

## Etapa 9 — Integração Asaas (billing da plataforma)
**Model: Sonnet 5 · Effort: high**

Implemente o app `billing`: model `Subscription`, criação de `customer` + `subscription` no
Asaas ao criar um `Tenant`, e endpoint `webhooks/asaas/` processado via Celery task
(idempotente — não processar o mesmo evento duas vezes — e validando a assinatura/token do
webhook conforme `04-INFRAESTRUTURA.md`). Trate ao menos `PAYMENT_CONFIRMED` e
`PAYMENT_OVERDUE`, atualizando `Subscription.status`. Deixe `grace_period_days` como campo
configurável, sem hardcode de regra de bloqueio — a regra final de quando bloquear acesso
por inadimplência é decisão de negócio a confirmar depois. Teste: idempotência do webhook e
atualização correta de status.

---

## Depois da Etapa 9

Nesse ponto o MVP completo (RF01-RF30 de `01-REQUISITOS.md`) está implementado. Antes de ir
para produção: revisar `04-INFRAESTRUTURA.md` (backup, firewall, SSL), rodar a suíte de
testes completa, e só então planejar a Fase 2 (relatórios, WhatsApp automático — RF31-RF34).
