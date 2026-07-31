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

# Arquivos nascem so para o dono: sem isto ha uma janela entre a criacao pelo
# shell e o chmod, em que o dump com dado pessoal fica legivel por qualquer um.
umask 077

mkdir -p "$DESTINO"
arquivo="$DESTINO/rastreio-$(date +%Y%m%d-%H%M%S).sql.gz"
# Escreve em temporario e so promove no fim. Um dump interrompido no meio
# produz um .gz com CONTEUDO -- que a verificacao de tamanho aprovaria, e que
# so se revelaria invalido na hora de restaurar.
parcial="$arquivo.parcial"

trap 'rm -f "$parcial"' EXIT

dc exec -T db pg_dump -U "$USUARIO" -d "$BANCO" --clean --if-exists \
    | gzip > "$parcial"

tamanho=$(stat -c%s "$parcial" 2>/dev/null || echo 0)
if [ "$tamanho" -lt 1000 ]; then
    echo "ERRO: backup vazio ou truncado ($tamanho bytes)" >&2
    exit 1
fi

# `gzip -t` detecta truncamento de verdade: um arquivo cortado no meio falha
# aqui mesmo tendo tamanho respeitavel.
if ! gzip -t "$parcial" 2>/dev/null; then
    echo "ERRO: arquivo gzip corrompido ou incompleto" >&2
    exit 1
fi

# O dump precisa conter as tabelas esperadas -- tamanho e integridade do gzip
# nao provam que o pg_dump chegou ao fim.
if ! gunzip -c "$parcial" | grep -q "consulta_log"; then
    echo "ERRO: dump nao contem consulta_log -- provavelmente incompleto" >&2
    exit 1
fi

# Promocao atomica: o arquivo final so passa a existir integro.
mv "$parcial" "$arquivo"
chmod 600 "$arquivo"

find "$DESTINO" -name 'rastreio-*.sql.gz' -mtime "+$RETENCAO_DIAS" -delete

echo "backup: $arquivo ($(du -h "$arquivo" | cut -f1))"
