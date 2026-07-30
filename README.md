# Zelo — Sistema de Agendamento e Gestão para Salões de Estética

*O zelo que seu salão merece.*

SaaS multi-tenant (path-based: `app.com/<slug-do-salao>`) para salões de beleza/estética
gerenciarem agendamento público, funcionários, serviços, comissões, estoque e caixa.

## Documentos deste projeto

| Arquivo | Conteúdo |
|---|---|
| [`01-REQUISITOS.md`](./01-REQUISITOS.md) | Requisitos funcionais, não-funcionais, personas, escopo (MVP vs fase 2) |
| [`02-ARQUITETURA.md`](./02-ARQUITETURA.md) | Stack, apps Django, estratégia multi-tenant, segurança, integrações |
| [`03-MODELO-DE-DADOS.md`](./03-MODELO-DE-DADOS.md) | Entidades, campos, relacionamentos, diagrama ER |
| [`04-INFRAESTRUTURA.md`](./04-INFRAESTRUTURA.md) | Docker Compose, Nginx, VPS, backups, deploy |
| [`CLAUDE.md`](./CLAUDE.md) | Regras, convenções, model/effort por etapa e skills recomendadas |
| [`05-PROMPT-INICIAL.md`](./05-PROMPT-INICIAL.md) | Etapa 1 — setup + núcleo multi-tenant (Opus 4.8) |
| [`06-PROMPTS-ETAPAS.md`](./06-PROMPTS-ETAPAS.md) | Etapas 2 a 9 — fatias verticais (Sonnet 5), um prompt por sessão |

## Como usar

1. Leia e valide `01-REQUISITOS.md` e `03-MODELO-DE-DADOS.md` — são a fonte da verdade do negócio.
2. Coloque **todos estes arquivos na raiz do repositório** (o `CLAUDE.md` é lido automaticamente
   pelo Claude Code a cada sessão).
3. Abra o Claude Code na pasta do projeto, rode `/model opus` e cole o conteúdo de
   `05-PROMPT-INICIAL.md`. Valide o resultado.
4. Para cada etapa seguinte, rode `/model sonnet` e cole o prompt correspondente de
   `06-PROMPTS-ETAPAS.md`, uma sessão por etapa, validando antes de avançar.

## Decisões já fechadas (não reabrir sem motivo forte)

- **Stack:** Python 3.12 + Django 5 + Django REST Framework
- **Frontend:** Django Templates + HTMX + Alpine.js (monolito, um único deploy)
- **Banco:** PostgreSQL 16
- **Multi-tenancy:** banco compartilhado, isolamento por `tenant_id` (row-level), roteamento por slug na URL
- **Fila/cache:** Redis + Celery (lembretes, alertas de estoque, webhooks do Asaas)
- **Pagamento da assinatura SaaS (tenant → plataforma):** Asaas
- **Infra:** VPS próprio, Docker Compose, Nginx, Certbot
- **Identificação do cliente final na página pública:** telefone (sem senha)
