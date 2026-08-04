#!/bin/bash
# Deploy do Zellup na VPS — chamado manualmente via SSH ou automaticamente pelo
# workflow do GitHub Actions (.github/workflows/deploy.yml) a cada push na main.
set -euo pipefail
cd "$(dirname "$0")"

git pull origin main

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

$COMPOSE build
$COMPOSE run --rm web python manage.py migrate --noinput
$COMPOSE run --rm web python manage.py collectstatic --noinput
$COMPOSE up -d

echo "Deploy concluído: $(date '+%Y-%m-%d %H:%M:%S')"
