# Prompt — Etapa 1: Setup + Núcleo Multi-tenant

**Model: Opus 4.8 · Effort: high (default)**

Copie tudo abaixo e cole como primeira mensagem no Claude Code, dentro da pasta do projeto
(com `CLAUDE.md`, `01-REQUISITOS.md`, `02-ARQUITETURA.md`, `03-MODELO-DE-DADOS.md` e
`04-INFRAESTRUTURA.md` já na raiz do repositório).

---

Você é o engenheiro responsável por construir o **Zelo**, um SaaS multi-tenant de
agendamento e gestão para salões de estética. Antes de escrever qualquer código, leia
integralmente `CLAUDE.md`, `01-REQUISITOS.md`, `02-ARQUITETURA.md`, `03-MODELO-DE-DADOS.md`
e `04-INFRAESTRUTURA.md` nesta pasta — eles são a fonte de verdade do projeto e você deve
seguir as convenções e regras de negócio ali descritas em toda decisão futura.

Esta é a **Etapa 1 de 9** do projeto (ver `CLAUDE.md` → seção "Model e Effort por etapa"
para saber o que usar nas próximas). Nesta etapa o foco é só infraestrutura + núcleo
multi-tenant — nenhuma feature de negócio ainda.

## O que fazer nesta sessão

1. **Inicializar o projeto:**
   - Estrutura Django conforme `02-ARQUITETURA.md` (`config/` + `apps/`).
   - `docker-compose.yml` + `docker-compose.override.yml` (dev) com os serviços descritos em
     `04-INFRAESTRUTURA.md` (web, db, redis, celery_worker, celery_beat).
   - `.env.example` com todas as variáveis listadas em `04-INFRAESTRUTURA.md`.
   - `requirements.txt` com: Django, djangorestframework, psycopg, celery, redis,
     django-environ (ou similar para .env), Pillow (upload de imagem), gunicorn.
   - Postgres como banco desde o dev (via Docker).

2. **Criar os apps vazios** conforme `02-ARQUITETURA.md`: `tenants`, `accounts`, `employees`,
   `services`, `scheduling`, `clients`, `inventory`, `finance`, `billing`, `public`.

3. **Implementar o núcleo multi-tenant:**
   - `TenantModel` abstrata + `TenantManager` (`apps/tenants/models.py`).
   - Model `Tenant` completo (campos de `03-MODELO-DE-DADOS.md`).
   - `User` customizado (`AbstractUser` + `role` + `tenant`), já configurado como
     `AUTH_USER_MODEL` — isso não dá pra trocar depois sem dor.
   - Middleware/contexto que resolve o tenant atual (por slug na URL pública, por
     `request.user.tenant` no painel logado).
   - **Escrever o teste de isolamento multi-tenant já nesta etapa** (dois tenants, garantir
     que query de um nunca retorna dado do outro). Esse teste é o "guarda-costas" do resto
     do projeto — não segue para a próxima etapa sem ele passando.

## Regras de execução
- Se alguma regra do `CLAUDE.md` ou `02-ARQUITETURA.md` parecer contraditória, pare e
  pergunte antes de assumir.
- Rode migrations e o teste de isolamento antes de considerar a etapa concluída.
- Ao final, me dê um resumo curto do que foi implementado e rode `docker compose up -d`
  para eu validar antes de seguirmos para a Etapa 2 (arquivo `06-PROMPTS-ETAPAS.md`).
