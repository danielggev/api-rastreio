#!/usr/bin/env bash
# Executa o painel de acompanhamento (deploy/monitor.sql) dentro do container
# do Postgres.
#
#   ./deploy/monitor.sh              # painel completo na tela
#   ./deploy/monitor.sh > painel.txt # para guardar ou enviar
#
# Somente leitura: nenhuma consulta do painel altera dados.

set -euo pipefail

RAIZ="${RAIZ:-/opt/rastreio}"
COMPOSE_FILE="${COMPOSE_FILE:-$RAIZ/deploy/docker-compose.traefik.yml}"
USUARIO="${POSTGRES_USER:-rastreio}"
BANCO="${POSTGRES_DB:-rastreio}"

# O `--env-file` e obrigatorio: sem ele o compose procura o .env ao lado do
# arquivo compose (em deploy/) e nao na raiz do projeto.
docker compose --env-file "$RAIZ/.env" -f "$COMPOSE_FILE" exec -T db \
    psql -U "$USUARIO" -d "$BANCO" -f - < "$RAIZ/deploy/monitor.sql"
