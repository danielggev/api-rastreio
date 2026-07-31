"""Expurgo LGPD -- roda diariamente via cron no VPS.

    0 4 * * *  cd /opt/rastreio && docker compose exec -T api python scripts/expurgar.py

Remove registros de auditoria alem da retencao de 90 dias e entradas de cache
vencidas.

IMPORTANTE: a retencao so se completa de fato apos o ciclo de backup. Copias
antigas ainda contem os registros apagados aqui -- por isso a retencao dos
backups precisa ser curta e estar documentada.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.db.session import criar_engine, criar_fabrica
from app.services.auditoria import RETENCAO_DIAS, Auditoria
from app.services.cache import CachePostgres


async def principal() -> int:
    s = get_settings()
    if not s.database_url:
        print("DATABASE_URL nao configurada; nada a expurgar.")
        return 1

    engine = criar_engine(s.database_url)
    fabrica = criar_fabrica(engine)

    try:
        logs = await Auditoria(fabrica).expurgar(RETENCAO_DIAS)
        cache = await CachePostgres(fabrica).expurgar()
    finally:
        await engine.dispose()

    print(f"consulta_log   : {logs} registro(s) com mais de {RETENCAO_DIAS} dias")
    print(f"rastreio_cache : {cache} entrada(s) vencida(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))
