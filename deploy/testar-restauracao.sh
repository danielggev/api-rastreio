#!/usr/bin/env bash
# Testa a RESTAURACAO de um backup num banco descartavel.
#
#   ./deploy/testar-restauracao.sh /opt/rastreio/backups/rastreio-20260731.sql.gz
#
# Backup nunca verificado costuma nao funcionar -- e a hora de descobrir isso
# nao e quando voce precisa dele. Este script restaura numa base temporaria,
# confere as tabelas e apaga tudo. NAO toca no banco de producao.

set -euo pipefail

ARQUIVO="${1:?informe o arquivo .sql.gz do backup}"
COMPOSE="${COMPOSE_FILE:-/opt/rastreio/deploy/docker-compose.prod.yml}"
USUARIO="${POSTGRES_USER:-rastreio}"
TEMP="teste_restauracao_$$"

if [ ! -s "$ARQUIVO" ]; then
    echo "ERRO: arquivo inexistente ou vazio: $ARQUIVO" >&2
    exit 1
fi

echo "Restaurando $ARQUIVO em uma base temporaria ($TEMP)..."

limpar() {
    docker compose -f "$COMPOSE" exec -T db \
        psql -U "$USUARIO" -d postgres -c "DROP DATABASE IF EXISTS $TEMP;" >/dev/null 2>&1 || true
}
trap limpar EXIT

docker compose -f "$COMPOSE" exec -T db \
    psql -U "$USUARIO" -d postgres -c "CREATE DATABASE $TEMP;" >/dev/null

gunzip -c "$ARQUIVO" | docker compose -f "$COMPOSE" exec -T db \
    psql -U "$USUARIO" -d "$TEMP" >/dev/null

tabelas=$(docker compose -f "$COMPOSE" exec -T db \
    psql -U "$USUARIO" -d "$TEMP" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")

registros=$(docker compose -f "$COMPOSE" exec -T db \
    psql -U "$USUARIO" -d "$TEMP" -tAc \
    "SELECT count(*) FROM consulta_log;" 2>/dev/null || echo "0")

echo "  tabelas restauradas : $tabelas"
echo "  registros em consulta_log : $registros"

if [ "$tabelas" -lt 2 ]; then
    echo "FALHOU: esperava ao menos consulta_log e rastreio_cache." >&2
    exit 1
fi

echo "OK -- o backup e restauravel."
