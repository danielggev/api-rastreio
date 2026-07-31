"""Consolidacao de campos que aparecem espalhados pelas ocorrencias.

Nem toda ocorrencia repete previsao de entrega, transportadora ou codigo de
volume. Tomar sempre o valor da ocorrencia mais recente APAGARIA dados validos:
uma ocorrencia nova com `data_prevista_entrega: null` sobrescreveria uma previsao
legitima registrada antes.

Regra: para cada campo, o valor NAO NULO mais recente de todo o historico.
"""

from __future__ import annotations

from datetime import date

from app.schemas import OcorrenciaFR


def _primeiro_nao_nulo[T](valores: list[T | None]) -> T | None:
    for v in valores:
        if v is not None and v != "":
            return v
    return None


def previsao_entrega(ordenadas: list[OcorrenciaFR]) -> date | None:
    """Previsao nao nula mais recente."""
    return _primeiro_nao_nulo([o.data_prevista_entrega for o in ordenadas])


def transportadora(ordenadas: list[OcorrenciaFR]) -> str | None:
    """Razao social nao nula mais recente."""
    return _primeiro_nao_nulo([o.razao_social_transportadora for o in ordenadas])


def codigo_volume(ordenadas: list[OcorrenciaFR]) -> str | None:
    """Codigo de volume nao nulo mais recente.

    Serve de fallback quando a Shopify nao tem tracking no fulfillment. A
    documentacao descreve o campo como eventual; as duas amostras reais nao o
    trouxeram, o que nao prova que nunca venha.
    """
    return _primeiro_nao_nulo([o.codigo_volume for o in ordenadas])
