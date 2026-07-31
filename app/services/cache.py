"""Cache das ocorrencias da Frete Rapido.

Duas regras que parecem detalhe e nao sao:

1. O cache so e CONSULTADO depois da validacao do email (ver `consulta.py`).
   Consultar antes devolveria dados de um pedido alheio a quem soubesse apenas
   o numero.
2. Retorno VAZIO nunca e armazenado. Cachear um vazio criaria um `vazio_fr`
   falso: se o rastreio surgisse durante o TTL, o cliente continuaria vendo
   "status indisponivel" e, ao atualizar a pagina, nada mudaria -- o que parece
   defeito grave.

Guardamos apenas o modelo normalizado (`OcorrenciaFR`), nunca o payload bruto:
o cru traz CPF/CNPJ e nome do entregador, dados pessoais de terceiros sem
utilidade para a resposta.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import RastreioCache
from app.db.session import sessao
from app.schemas import OcorrenciaFR

logger = logging.getLogger(__name__)

TTL_PADRAO_S = 15 * 60


class Cache(Protocol):
    async def obter(self, chave: str) -> list[OcorrenciaFR] | None: ...

    async def guardar(self, chave: str, ocorrencias: list[OcorrenciaFR]) -> None: ...


class CacheMemoria:
    """Implementacao em memoria. A Fase 2 troca por PostgreSQL."""

    def __init__(self, ttl_s: int = TTL_PADRAO_S) -> None:
        self._ttl = ttl_s
        self._dados: dict[str, tuple[float, list[OcorrenciaFR]]] = {}

    async def obter(self, chave: str) -> list[OcorrenciaFR] | None:
        item = self._dados.get(chave)
        if item is None:
            return None
        gravado_em, ocorrencias = item
        if time.monotonic() - gravado_em > self._ttl:
            # Remove de fato o expirado, em vez de apenas ignorar: registro
            # vencido nao deve continuar ocupando espaco.
            del self._dados[chave]
            return None
        return ocorrencias

    async def guardar(self, chave: str, ocorrencias: list[OcorrenciaFR]) -> None:
        if not ocorrencias:
            return
        self._dados[chave] = (time.monotonic(), ocorrencias)


class CacheDesligado:
    async def obter(self, chave: str) -> list[OcorrenciaFR] | None:
        return None

    async def guardar(self, chave: str, ocorrencias: list[OcorrenciaFR]) -> None:
        return None


class CachePostgres:
    """Cache persistente. Sobrevive a reinicio e e compartilhado entre workers.

    Guarda apenas o modelo NORMALIZADO -- `model_dump` do `OcorrenciaFR`, que ja
    passou pela lista de permissao. O payload bruto da Frete Rapido traz
    `cnpj_cpf_entregador` e `nome_entregador`, dados pessoais de terceiros que
    nao teriam utilidade alguma aqui.
    """

    def __init__(
        self,
        fabrica: async_sessionmaker[AsyncSession],
        ttl_s: int = TTL_PADRAO_S,
    ) -> None:
        self._fabrica = fabrica
        self._ttl = ttl_s

    async def obter(self, chave: str) -> list[OcorrenciaFR] | None:
        limite = datetime.now(UTC) - timedelta(seconds=self._ttl)
        try:
            async with sessao(self._fabrica) as sess:
                registro = await sess.get(RastreioCache, chave)
                if registro is None:
                    return None
                if registro.atualizado_em < limite:
                    # Remove de fato o expirado, em vez de apenas ignorar:
                    # registro vencido nao deve continuar ocupando espaco nem
                    # aparecer em backup.
                    await sess.delete(registro)
                    return None
                return [
                    OcorrenciaFR.model_validate(o) for o in registro.ocorrencias
                ]
        except Exception as exc:
            # Cache indisponivel nao pode impedir a consulta: seguimos para a
            # Frete Rapido como se fosse um miss.
            logger.warning("falha ao ler o cache: %s", exc)
            return None

    async def guardar(self, chave: str, ocorrencias: list[OcorrenciaFR]) -> None:
        if not ocorrencias:
            return
        try:
            dados = [o.model_dump(mode="json") for o in ocorrencias]
            async with sessao(self._fabrica) as sess:
                registro = await sess.get(RastreioCache, chave)
                if registro is None:
                    sess.add(
                        RastreioCache(
                            numero_pedido=chave,
                            ocorrencias=dados,
                            atualizado_em=datetime.now(UTC),
                        )
                    )
                else:
                    registro.ocorrencias = dados
                    registro.atualizado_em = datetime.now(UTC)
        except Exception as exc:
            logger.warning("falha ao gravar no cache: %s", exc)

    async def expurgar(self) -> int:
        """Remove registros vencidos. Roda junto com o expurgo do log."""
        limite = datetime.now(UTC) - timedelta(seconds=self._ttl)
        async with sessao(self._fabrica) as sess:
            resultado = await sess.execute(
                delete(RastreioCache).where(RastreioCache.atualizado_em < limite)
            )
        return int(getattr(resultado, "rowcount", 0) or 0)
