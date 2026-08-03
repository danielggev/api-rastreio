"""Expurgo LGPD -- roda diariamente via cron no VPS.

    0 4 * * *  cd /opt/rastreio && docker compose exec -T api python scripts/expurgar.py

Remove registros de auditoria alem da retencao de 90 dias, entradas de cache
vencidas e eventos antigos do webhook.

`evento_frete` nao tem dado pessoal -- ali o expurgo e controle de VOLUME, nao
exigencia legal: gravamos todo evento de todo pedido, o que cresce bem mais
rapido que `consulta_log`.

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
from app.services.eventos import EventosPostgres


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
        eventos = await EventosPostgres(fabrica).expurgar(RETENCAO_DIAS)
    finally:
        await engine.dispose()

    print(f"consulta_log   : {logs} registro(s) com mais de {RETENCAO_DIAS} dias")
    print(f"rastreio_cache : {cache} entrada(s) vencida(s)")
    print(f"evento_frete   : {eventos} evento(s) com mais de {RETENCAO_DIAS} dias")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))
