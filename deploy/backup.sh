#!/usr/bin/env bash
# Backup do PostgreSQL.
#
#   0 3 * * *  /opt/rastreio/deploy/backup.sh
#
# O banco guarda apenas log de auditoria e cache -- nenhum dado de negocio
# insubstituivel. Por isso a retencao e CURTA: as copias contem dado pessoal
# pseudonimizado, e o expurgo de 90 dias so se completa de fato depois que os
# backups antigos saem de circulacao.

set -euo pipefail

RAIZ="${RAIZ:-/opt/rastreio}"
COMPOSE_FILE="${COMPOSE_FILE:-$RAIZ/deploy/docker-compose.traefik.yml}"
DESTINO="${BACKUP_DIR:-$RAIZ/backups}"
RETENCAO_DIAS="${BACKUP_RETENCAO_DIAS:-30}"
USUARIO="${POSTGRES_USER:-rastreio}"
BANCO="${POSTGRES_DB:-rastreio}"

# O `--env-file` e obrigatorio: sem ele o compose procura o .env ao lado do
# arquivo compose (em deploy/) e falha antes de chegar ao pg_dump.
dc() { docker compose --env-file "$RAIZ/.env" -f "$COMPOSE_FILE" "$@"; }

mkdir -p "$DESTINO"
arquivo="$DESTINO/rastreio-$(date +%Y%m%d-%H%M%S).sql.gz"

# Falha no meio nao pode deixar um arquivo pela metade: um backup truncado
# passa por bom ate a hora em que voce precisa restaurar.
trap '[ -f "$arquivo" ] && [ ! -s "$arquivo" ] && rm -f "$arquivo"; exit 1' ERR

dc exec -T db pg_dump -U "$USUARIO" -d "$BANCO" --clean --if-exists \
    | gzip > "$arquivo"

tamanho=$(stat -c%s "$arquivo" 2>/dev/null || echo 0)
if [ "$tamanho" -lt 1000 ]; then
    echo "ERRO: backup vazio ou truncado ($tamanho bytes)" >&2
    rm -f "$arquivo"
    exit 1
fi

chmod 600 "$arquivo"
find "$DESTINO" -name 'rastreio-*.sql.gz' -mtime "+$RETENCAO_DIAS" -delete

echo "backup: $arquivo ($(du -h "$arquivo" | cut -f1))"
