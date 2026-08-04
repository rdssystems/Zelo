# CLAUDE.md — Regras do projeto Zellup

Este arquivo é lido automaticamente pelo agente em toda sessão. Ele define como trabalhar
neste repositório. Documentos de referência (leia antes de decisões grandes):
`01-REQUISITOS.md`, `02-ARQUITETURA.md`, `03-MODELO-DE-DADOS.md`, `04-INFRAESTRUTURA.md`.

## Stack (não mudar sem confirmar com o usuário)
Python 3.12 · Django 5 · Django REST Framework · PostgreSQL 16 · HTMX + Alpine.js ·
Celery + Redis · Docker Compose · Asaas (billing).

## Regras de negócio inegociáveis
1. **Isolamento multi-tenant é a regra #1.** Toda query em model tenant-aware deve ser
   filtrada por `tenant`. Nunca escrever uma view/queryset que possa vazar dado entre tenants.
   Todo novo model tenant-aware herda de `TenantModel` (ver `apps/tenants/models.py`).
2. **Nunca editar `Product.current_stock` diretamente.** Toda mudança de estoque passa por um
   `StockMovement`, dentro de uma função de domínio (`services.py`), nunca direto no admin ou
   numa view solta.
3. **Conclusão de agendamento é uma operação atômica** (`transaction.atomic()`): gera
   `Commission` + `CashTransaction` + (se aplicável) `StockMovement`, tudo ou nada.
4. **Cálculo de comissão:** prioridade `EmployeeService` (override) > `Employee` (padrão).
   Sempre salvar snapshot (`commission_type`, `commission_value`, `base_amount`) na
   `Commission` gerada — não recalcular depois com base em dado que pode ter mudado.
5. **Disponibilidade de agenda** = jornada (`WorkingHours`) − agendamentos existentes
   (pending/confirmed) − exceções (`ScheduleException`). Toda essa lógica fica centralizada
   em `apps/scheduling/availability.py` — não duplicar cálculo de slot livre em outro lugar.
6. **Cliente final nunca precisa de senha.** Identificação é por telefone, escopado ao tenant
   (`unique_together = (tenant, phone)`).

## Convenções de código
- Regra de negócio vive em `apps/<app>/services.py` (funções puras/testáveis), **não** em
  `views.py` nem inflando `models.py`. Views chamam services.
- Um `serializers.py` e `views.py` (DRF) por app, mesmo que o front use HTMX — a API REST
  deve sempre refletir o que o painel faz (usada futuramente pelo app mobile).
- Nomes de model, campo e variável em **inglês**; textos visíveis ao usuário (templates,
  mensagens, labels) em **português (pt-BR)**.
- Toda `Decimal` para dinheiro/quantidade — nunca `float`.
- Toda model financeira/estoque tem `created_by` e `created_at`.
- Migrations sempre revisadas manualmente antes de aplicar em produção (ver se não vai travar
  tabela grande, ver default de campo novo em tabela com dado).
- Testes obrigatórios para: isolamento multi-tenant, cálculo de comissão, baixa de estoque,
  disponibilidade de agenda. Não é "nice to have" — é bloqueante para merge.

## Fluxo de trabalho esperado do agente
- Antes de criar um app/model novo, checar se já não existe algo parecido em
  `03-MODELO-DE-DADOS.md` — o modelo de dados é a fonte da verdade, atualizar o `.md` se o
  modelo mudar durante a implementação.
- Construir **incrementalmente**, por fatia vertical (ex: "cadastro de serviço" completo —
  model + admin + view + template — antes de partir pra próxima fatia), não por camada.
- Rodar migrations e testes antes de considerar uma etapa concluída.
- Não introduzir dependência nova sem necessidade clara (mantemos o projeto enxuto).
- Sempre que uma decisão de negócio não estiver clara nos `.md`, **perguntar antes de assumir**
  (especialmente em regras de cobrança, comissão e estoque — erro ali é dinheiro real).

## O que NÃO fazer
- Não usar `float` para dinheiro.
- Não fazer queries sem filtro de tenant "porque só tem um tenant de teste por enquanto".
- Não guardar segredo (API key Asaas, `SECRET_KEY`) em código — sempre `.env`.
- Não implementar relatórios avançados ou notificação WhatsApp automática agora — isso é
  fase 2 (RF31-RF34 em `01-REQUISITOS.md`). Só deixar o modelo de dados preparado.
- Não misturar lógica de HTMX/template com lógica de cálculo financeiro — cálculo sempre em
  `services.py`, template só exibe.

## Model e Effort por etapa (Claude Code)

`/model` define a capacidade base; `/effort` define quanto raciocínio ele aplica naquele turno.
São independentes — ajustar por etapa, não deixar fixo no máximo.

| Etapa | Model | Effort | Por quê |
|---|---|---|---|
| Núcleo multi-tenant (`TenantModel`, `User` custom, middleware de tenant) | Opus 4.8 | high (default) | Decisão arquitetural difícil de reverter depois |
| Motor de disponibilidade de agenda (`scheduling/availability.py`) | Sonnet 5 | high | Lógica sutil (jornada − ocupado − exceções), erro aqui quebra o agendamento |
| Conclusão de atendimento (comissão + caixa + estoque, atômico) | Sonnet 5 | high | Mexe com dinheiro, erro é caro |
| Integração Asaas (billing) + webhook | Sonnet 5 | high | Idempotência e validação de assinatura exigem cuidado |
| CRUD simples (serviços, funcionário, configurações) | Sonnet 5 | medium | Tarefa bem especificada, medium já acerta de primeira |
| Templates/CSS da página pública (usar skill `frontend-design`) | Sonnet 5 | low/medium | Ajuste visual, baixo risco |

Regra prática: comece em `medium`; se precisar reprompt­ar mais de uma vez no mesmo tipo de
tarefa, suba o effort — não reescreva o prompt. Evite trocar model/effort no meio de uma
sessão em andamento (perde cache, fica mais caro); troque entre uma etapa e outra.

## Skills recomendadas (comunidade, instalar em `.claude/skills/`)

Não são skills oficiais da Anthropic — revisar o conteúdo antes de instalar, como qualquer
dependência de terceiro.

- **`django-expert`** — models/ORM, DRF, viewsets, testes, migrations. Cobre a maior parte da
  implementação deste projeto.
- **`django-security`** — CSRF, autenticação, permissões, hardening de produção. Importante
  porque o projeto tem multi-tenant + dado financeiro.
- **`frontend-design`** (já instalada) — usar nas etapas de página pública e painel.

## Infraestrutura da VPS (servidor compartilhado)

O deploy acontece num VPS **compartilhado com outros produtos** (não exclusivo do Zellup).
Inventário completo (hardware, containers, firewall, capacidade) e instruções de acesso ficam
em `VPS-INFRAESTRUTURA-ATUAL.md` na raiz do projeto — **arquivo local, não versionado**
(contém IP/porta/topologia). Antes de qualquer tarefa de infraestrutura/deploy/SSH nesse
servidor, ler esse arquivo inteiro primeiro; depois de qualquer mudança feita na VPS,
atualizá-lo. Se o arquivo não existir na sua sessão, perguntar ao usuário os dados de acesso
antes de assumir.

## Deploy (produção, implantado em 2026-08-04)

Push em `main` no GitHub (`rdssystems/Zelo`) dispara deploy automático via GitHub Actions
(`.github/workflows/deploy.yml` → SSH restrito → `atualizar.sh` na VPS: `git pull` + build +
migrate + collectstatic + `up -d`). Detalhe técnico completo em `04-INFRAESTRUTURA.md` §5.

**Antes de mesclar em `main` mudança não-trivial** (não só um typo/CSS): testar numa branch
separada. Como nem toda máquina de trabalho tem Docker local, o jeito confiável é testar direto
na VPS sem afetar produção — o `docker-compose.prod.yml` não monta o código via bind (só
`docker-compose.override.yml`, que é só dev), então trocar de branch no checkout da VPS e
buildar uma imagem de teste não mexe nos containers já rodando:
```bash
ssh -p 22022 root@<IP>   # ver VPS-INFRAESTRUTURA-ATUAL.md pro IP
cd /root/zelo && git stash && git fetch origin && git checkout <branch>
docker compose -f docker-compose.yml -f docker-compose.prod.yml build web
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm web python manage.py test
git checkout main && git stash pop   # restaura o checkout de produção antes de sair
```
Só depois de testes passando: mesclar a branch em `main` localmente e `git push origin main` —
esse push já dispara o deploy de verdade.

## Comandos úteis
```bash
docker compose up -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py test
docker compose exec web python manage.py shell
```
