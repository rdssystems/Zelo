# Infraestrutura — Zellup

## 1. Visão geral

Deploy em **VPS próprio**, via Docker Compose, com Nginx como proxy reverso + SSL (Certbot).

```
Internet → Nginx (80/443, SSL) → Gunicorn (Django) → Postgres
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
| `nginx` | `nginx:alpine` | proxy reverso, serve `static/`, TLS |
| `certbot` | `certbot/certbot` | renovação automática de certificado |

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

## 5. Deploy — passo a passo (produção)

1. `git pull` no VPS (ou pipeline CI simples via GitHub Actions + SSH deploy).
2. `docker compose -f docker-compose.yml -f docker-compose.prod.yml build`
3. `docker compose ... run --rm web python manage.py migrate`
4. `docker compose ... run --rm web python manage.py collectstatic --noinput`
5. `docker compose ... up -d`
6. Healthcheck simples em `/healthz/` (endpoint leve, sem tocar banco pesado, para monitoramento).

Recomendo, quando o projeto estabilizar, configurar um pipeline no GitHub Actions
(build → testes → deploy via SSH) em vez de deploy manual — mas isso pode entrar depois do MVP.

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
