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

DESTINO="${BACKUP_DIR:-/opt/rastreio/backups}"
RETENCAO_DIAS="${BACKUP_RETENCAO_DIAS:-30}"
COMPOSE="${COMPOSE_FILE:-/opt/rastreio/deploy/docker-compose.prod.yml}"
USUARIO="${POSTGRES_USER:-rastreio}"
BANCO="${POSTGRES_DB:-rastreio}"

mkdir -p "$DESTINO"
arquivo="$DESTINO/rastreio-$(date +%Y%m%d-%H%M%S).sql.gz"

docker compose -f "$COMPOSE" exec -T db \
    pg_dump -U "$USUARIO" -d "$BANCO" --clean --if-exists \
    | gzip > "$arquivo"

# Um dump vazio ou truncado passa despercebido ate a hora em que voce precisa
# restaurar. Falhar aqui e melhor do que descobrir depois.
if [ ! -s "$arquivo" ] || [ "$(stat -c%s "$arquivo")" -lt 1000 ]; then
    echo "ERRO: backup gerado esta vazio ou truncado: $arquivo" >&2
    rm -f "$arquivo"
    exit 1
fi

chmod 600 "$arquivo"
find "$DESTINO" -name 'rastreio-*.sql.gz' -mtime "+$RETENCAO_DIAS" -delete

echo "backup: $arquivo ($(du -h "$arquivo" | cut -f1))"
