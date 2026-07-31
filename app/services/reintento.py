"""Reintento apenas para falhas transitorias, com orcamento total de tempo.

Repetir um 400 ou 404 e inutil e so amplifica carga. E do outro lado ha uma
pessoa esperando a pagina responder, entao existe um teto de tempo total, nao
apenas um numero de tentativas.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.services.logs import redigir_excecao

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Politica:
    max_tentativas: int = 3  # 1 chamada + 2 reintentos
    orcamento_s: float = 8.0
    base_espera_s: float = 0.3
    # Teto de CADA chamada. Sem isto o orcamento total nao se sustenta: contar
    # apenas a espera entre tentativas permitia que uma unica chamada travasse
    # por 10s e estourasse sozinha o limite.
    timeout_chamada_s: float = 6.0

    def restante(self, decorrido: float) -> float:
        """Quanto tempo a proxima chamada ainda pode consumir."""
        return max(self.orcamento_s - decorrido, 0.0)

    def timeout_para(self, decorrido: float) -> float:
        """Timeout da proxima chamada, limitado pelo que sobra do orcamento."""
        return max(min(self.timeout_chamada_s, self.restante(decorrido)), 0.5)


class Transitorio(Exception):
    """Falha que vale a pena repetir (timeout, conexao, 429, 5xx, THROTTLED)."""


class Permanente(Exception):
    """Falha que NAO deve ser repetida (400, 404, credencial invalida)."""


async def com_reintento[T](
    operacao: Callable[[float], Awaitable[T]],
    politica: Politica | None = None,
    *,
    nome: str = "chamada externa",
) -> T:
    """Executa `operacao(timeout)` com reintento dentro de um orcamento total.

    A operacao RECEBE o timeout que pode gastar. E o que faz o orcamento valer
    de verdade: sem passar esse limite adiante, cada chamada usaria o proprio
    timeout e o total viraria a soma deles.
    """
    pol = politica or Politica()
    inicio = time.monotonic()
    ultima: Exception | None = None

    for tentativa in range(1, pol.max_tentativas + 1):
        decorrido = time.monotonic() - inicio
        if decorrido >= pol.orcamento_s and ultima is not None:
            break
        try:
            return await operacao(pol.timeout_para(decorrido))
        except Permanente:
            raise
        except Transitorio as exc:
            ultima = exc
            decorrido = time.monotonic() - inicio
            espera = pol.base_espera_s * (2 ** (tentativa - 1))

            # Nao inicia uma espera que estouraria o orcamento: melhor devolver
            # erro agora do que deixar o cliente olhando a tela girar.
            if tentativa == pol.max_tentativas or decorrido + espera >= pol.orcamento_s:
                break

            logger.warning(
                "%s falhou (tentativa %d/%d), repetindo em %.1fs: %s",
                nome,
                tentativa,
                pol.max_tentativas,
                espera,
                redigir_excecao(exc),
            )
            await asyncio.sleep(espera)

    assert ultima is not None
    raise ultima
