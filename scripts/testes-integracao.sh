#!/usr/bin/env bash
#
# Testes de concorrencia contra Postgres REAL.
#
#     ./scripts/testes-integracao.sh
#
# Existe porque a suite normal usa `EventosMemoria`, onde `adquirir` nao tem
# `await` interno e portanto e atomico por construcao: ela prova a logica do
# servico, nao o SQL. O `pg_advisory_xact_lock`, o `ON CONFLICT`, o fencing do
# lease e o `NULLS NOT DISTINCT` so aparecem com banco de verdade.
#
# Sobe um Postgres descartavel numa porta propria (55432, para nao encostar no
# banco da aplicacao) e roda os testes num container tambem descartavel -- a
# imagem da API nao tem pytest, porque o Dockerfile instala so as dependencias
# de runtime.
#
# NAO toca no banco de producao. O container de teste e removido ao final,
# inclusive se algo falhar no meio.

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORTA="${PORTA_TESTE:-55432}"
NOME="pg-teste-rastreio"

limpar() {
    docker rm -f "$NOME" >/dev/null 2>&1 || true
}
trap limpar EXIT

echo "==> subindo Postgres descartavel na porta $PORTA"
limpar
docker run --rm -d --name "$NOME" --network host \
    -e POSTGRES_PASSWORD=teste \
    -e POSTGRES_DB=rastreio_teste \
    -e PGPORT="$PORTA" \
    postgres:17-alpine >/dev/null

echo "==> aguardando o banco aceitar conexao"
for _ in $(seq 1 30); do
    if docker exec "$NOME" pg_isready -p "$PORTA" -U postgres >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "==> rodando os testes"
docker run --rm --network host \
    -v "$RAIZ:/app" -w /app \
    -e DATABASE_URL_TESTE="postgresql+psycopg://postgres:teste@localhost:$PORTA/rastreio_teste" \
    -e PIP_ROOT_USER_ACTION=ignore \
    python:3.12-slim \
    bash -c "pip install --quiet '.[dev]' && python -m pytest tests/test_eventos_integracao.py -v"
