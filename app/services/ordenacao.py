"""Ordenacao das ocorrencias, com desempate estavel.

Nas fixtures REAIS os codigos 0 ("Contratado") e 1 ("Aguardando coleta") tem
timestamps identicos -- em `resposta-59483.json`, ambos `2026-07-22 18:02:47`.
Ordenar so por data e nao deterministico e pode exibir "Contratado" como status
atual, escondendo que o pedido ja avancou.

O desempate e o indice em que a API entregou cada ocorrencia.
"""

from __future__ import annotations

from datetime import datetime

from app.schemas import OcorrenciaFR

# Ocorrencia sem data vai para o fim da lista decrescente, nunca para o topo:
# um registro sem timestamp nao deve virar o status atual.
_SEM_DATA = datetime.min


def _comparavel(bruta: datetime | None) -> datetime:
    """Forma naive, para que a ordenacao nunca compare aware com naive.

    A Frete Rapido manda datas SEM fuso (`"2026-07-23 15:37:12"`), e todo o
    projeto assume isso. Mas o schema aceita as duas formas, e basta uma unica
    ocorrencia com offset no meio de outras sem para o `sorted` estourar
    `TypeError` -- derrubando junto a pagina de rastreio, que usa esta mesma
    funcao.

    Aqui so precisamos de ordem total consistente, nao do instante correto: a
    atribuicao de fuso acontece depois, em `datas.atribuir_fuso`.
    """
    if bruta is None:
        return _SEM_DATA
    return bruta.replace(tzinfo=None) if bruta.tzinfo is not None else bruta


def _chave(o: OcorrenciaFR) -> tuple[datetime, datetime, int]:
    return (
        _comparavel(o.data_ocorrencia),
        _comparavel(o.data_atualizacao),
        o.indice_origem,
    )


def ordenar_desc(ocorrencias: list[OcorrenciaFR]) -> list[OcorrenciaFR]:
    """Mais recente primeiro; `resultado[0]` e o status atual.

    `reverse=True` inverte tambem o indice de origem, e isso e exatamente o
    desejado: entre ocorrencias do mesmo instante, a que a API listou POR ULTIMO
    e a mais avancada no fluxo.
    """
    return sorted(ocorrencias, key=_chave, reverse=True)


def indexar(ocorrencias: list[OcorrenciaFR]) -> list[OcorrenciaFR]:
    """Grava a posicao original de cada ocorrencia, antes de qualquer ordenacao."""
    for i, o in enumerate(ocorrencias):
        o.indice_origem = i
    return ocorrencias
