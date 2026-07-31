"""Rate limiting por IP.

E a defesa PRINCIPAL contra enumeracao de pedidos. CORS nao serve para isso:
restringe apenas navegadores, e qualquer script ou servidor o ignora.

Implementacao propria em vez de `slowapi` por dois motivos: precisamos que a
chave venha da deteccao de proxy confiavel (ver `ip_cliente.py`), e a janela
deslizante cabe em poucas linhas sem trazer outra dependencia.

NOTA SOBRE WORKERS: a contagem e por processo. Com N workers uvicorn, o limite
efetivo seria N vezes maior. A centenas de consultas por dia, `workers=1`
resolve com folga -- e essa escolha esta documentada no deploy. Se o volume
crescer, trocar por Redis.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class LimitadorJanelaDeslizante:
    """Permite `limite` requisicoes por `janela_s` para cada chave."""

    def __init__(self, limite: int = 10, janela_s: int = 60) -> None:
        self._limite = limite
        self._janela = janela_s
        self._marcas: defaultdict[str, deque[float]] = defaultdict(deque)

    def permitir(self, chave: str, agora: float | None = None) -> bool:
        t = agora if agora is not None else time.monotonic()
        marcas = self._marcas[chave]

        limite_inferior = t - self._janela
        while marcas and marcas[0] <= limite_inferior:
            marcas.popleft()

        if len(marcas) >= self._limite:
            return False

        marcas.append(t)
        return True

    def restantes(self, chave: str, agora: float | None = None) -> int:
        t = agora if agora is not None else time.monotonic()
        marcas = self._marcas[chave]
        limite_inferior = t - self._janela
        validas = sum(1 for m in marcas if m > limite_inferior)
        return max(self._limite - validas, 0)

    def limpar(self, agora: float | None = None) -> int:
        """Descarta chaves sem marcas validas, para a memoria nao crescer sem fim."""
        t = agora if agora is not None else time.monotonic()
        limite_inferior = t - self._janela
        vazias = [
            chave
            for chave, marcas in self._marcas.items()
            if not marcas or marcas[-1] <= limite_inferior
        ]
        for chave in vazias:
            del self._marcas[chave]
        return len(vazias)


def interpretar_limite(texto: str) -> tuple[int, int]:
    """Converte "10/minute" em (10, 60). Aceita second, minute e hour."""
    unidades = {"second": 1, "minute": 60, "hour": 3600}
    try:
        quantidade, unidade = texto.split("/", 1)
        return (int(quantidade.strip()), unidades[unidade.strip().lower()])
    except (ValueError, KeyError):
        raise ValueError(
            f"formato de RATE_LIMIT invalido: {texto!r} (ex.: '10/minute')"
        ) from None
