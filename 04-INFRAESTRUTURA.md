# Infraestrutura — Zellup

## 1. Visão geral

Deploy em VPS **compartilhada com outros produtos** (não exclusiva do Zellup — ver
`VPS-INFRAESTRUTURA-ATUAL.md`, arquivo local não versionado, pra inventário completo). Entrada
pública via **nginx-proxy-manager** (NPM, container à parte, fora do `docker-compose.yml` do
Zellup — já servia outros domínios do host antes do Zellup existir), não um `nginx`/`certbot`
dedicado no compose do projeto.

```
Internet → Cloudflare (Full/strict) → nginx-proxy-manager (80/443, Let's Encrypt)
         → Gunicorn (Django, container "web") → Postgres
                                               → Redis → Celery worker / beat
```

## 2. Serviços no `docker-compose.yml`

| Serviço | Imagem/base | Função |
|---|---|---|
| `web` | build próprio (Python 3.12) | Django + Gunicorn |
| `celery_worker` | mesma imagem do `web` | processa tasks assíncronas (webhooks Asaas, jobs) |
| `celery_beat` | mesma imagem do `web` | agenda tasks periódicas (ex: checar assinaturas vencidas) |
| `db` | `postgres:16` | banco principal |
| `redis` | `redis:7` | broker do Celery + cache |

Estático é servido pelo próprio Gunicorn via **Whitenoise** (`config/settings.py::STORAGES`) —
não tem serviço `nginx`/`static` dedicado. TLS e proxy reverso ficam no **nginx-proxy-manager**,
que não faz parte deste `docker-compose.yml` (é da VPS, compartilhado — ver seção 5).

## 3. Ambientes

- `.env.dev` — desenvolvimento local (pode usar SQLite ou Postgres via Docker; recomendado
  já usar Postgres em dev para evitar surpresa em produção).
- `.env.prod` — produção, nunca commitado (adicionar ao `.gitignore`).
- `docker-compose.yml` (base) + `docker-compose.override.yml` (dev) +
  `docker-compose.prod.yml` (produção) — padrão de composição por ambiente.

## 4. Variáveis de ambiente (mínimo)

```
DJANGO_SECRET_KEY=
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=app.seudominio.com.br
DATABASE_URL=postgres://user:pass@db:5432/belezaapp
REDIS_URL=redis://redis:6379/0
ASAAS_API_KEY=
ASAAS_WEBHOOK_TOKEN=
ASAAS_ENV=sandbox   # ou production
DEFAULT_FROM_EMAIL=
EMAIL_HOST= / EMAIL_PORT= / EMAIL_HOST_USER= / EMAIL_HOST_PASSWORD=
MEDIA_ROOT=/app/media
STATIC_ROOT=/app/static
```

## 5. Deploy — automático via GitHub Actions (implantado em 2026-08-04)

Todo push em `main` do repo (`github.com/rdssystems/Zelo`) dispara o workflow
`.github/workflows/deploy.yml`, que conecta via SSH na VPS e roda `atualizar.sh`
(`/root/zelo/atualizar.sh`), que faz:
1. `git pull origin main`.
2. `docker compose -f docker-compose.yml -f docker-compose.prod.yml build`
3. `... run --rm web python manage.py migrate --noinput`
4. `... run --rm web python manage.py collectstatic --noinput`
5. `... up -d`

A chave SSH usada pelo workflow é **dedicada** (não a de acesso pessoal), restrita no
`authorized_keys` da VPS via `command="/root/zelo/atualizar.sh"` — mesmo que o secret do GitHub
vaze, só dá pra rodar esse script, nada além disso. Healthcheck em `/healthz/` (endpoint leve,
sem tocar banco pesado) pra monitoramento.

**Prática adotada pra mudanças não-triviais** (evita testar direto em produção, já que não há
Docker local disponível em toda máquina de trabalho):
1. Commitar numa branch separada (não `main`) e dar push.
2. Na VPS, `git checkout` dessa branch **sem afetar os containers em produção** — o
   `docker-compose.prod.yml` não usa bind-mount do código (diferente do `docker-compose.override.yml`
   de dev), então trocar a branch no disco não muda o que já está rodando.
3. Buildar uma imagem de teste isolada (`docker compose ... build web`) e rodar
   `... run --rm web python manage.py test` contra o Postgres/Redis reais da VPS, num container
   descartável — valida com fidelidade total ao ambiente de produção, sem arriscá-lo.
4. Se os testes passarem: `git checkout main` de volta na VPS (restaura o checkout de produção),
   mesclar a branch em `main` localmente, `git push origin main` — o passo 4 sozinho já dispara
   o deploy automático de verdade.

Deploy manual (sem passar pelo Actions) continua possível rodando `atualizar.sh` direto na VPS
via SSH, se precisar.

## 6. Backups

- **Banco:** `pg_dump` diário automatizado (cron no host ou serviço no compose), retenção
  mínima de 7 dias local + envio para armazenamento externo (ex: bucket S3-compatible, mesmo
  que barato) — dado financeiro não pode depender só do disco do VPS.
- **Mídia** (`media/` — logos, fotos, fundo de página pública): backup incremental junto com
  o banco ou sync para bucket externo.

## 7. Segurança de infraestrutura

- Firewall no VPS: só 22 (SSH, idealmente com chave e fail2ban), 80 e 443 abertos.
- Postgres e Redis **não expostos publicamente** (apenas na rede interna do Docker Compose).
- Certificado SSL renovado automaticamente via Certbot (cron/timer).
- Atualizações de segurança do SO agendadas.

## 8. Monitoramento mínimo viável
- Logs do Gunicorn/Nginx com rotação (`logrotate`).
- Sentry (ou similar) plugado ao Django para captura de exceptions em produção — recomendado
  assim que o MVP estiver no ar.
- Alerta simples (e-mail ou webhook) se o healthcheck cair.

## 9. Escalabilidade (não é prioridade agora, mas deixar caminho aberto)
- Como o isolamento é por `tenant_id` num banco só, crescer verticalmente (mais CPU/RAM no
  VPS, tuning de Postgres) resolve por bastante tempo.
- Se um dia for necessário separar tenants grandes, a modelagem já permite migrar tenants
  específicos para um banco dedicado sem redesenhar o schema.
