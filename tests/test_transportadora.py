"""Nome de exibicao da transportadora.

A Frete Rapido devolve razao social; o cliente reconhece o nome comercial.
"""

from __future__ import annotations

import pytest

from app.services.transportadora import nome_amigavel


@pytest.mark.parametrize(
    ("razao_social", "esperado"),
    [
        # Casos REAIS, vistos nos pedidos 59551 e 59552.
        ("EMPRESA BRASILEIRA DE CORREIOS E TELEGRAFOS", "Correios"),
        ("JADLOG LOGISTICA S.A", "Jadlog"),
        # Outras transportadoras comuns no e-commerce brasileiro.
        ("TOTAL EXPRESS ENCOMENDAS LTDA", "Total Express"),
        ("LOGGI TECNOLOGIA LTDA", "Loggi"),
        ("AZUL LINHAS AEREAS BRASILEIRAS S/A", "Azul Cargo"),
        ("BRASPRESS TRANSPORTES URGENTES LTDA", "Braspress"),
        ("DHL EXPRESS BRASIL", "DHL"),
        ("FEDEX BRASIL LOGISTICA", "FedEx"),
    ],
)
def test_transportadoras_conhecidas(razao_social: str, esperado: str) -> None:
    assert nome_amigavel(razao_social) == esperado


def test_desconhecida_perde_sufixo_societario_e_caixa_alta() -> None:
    """Transportadora nova nao pode aparecer gritando em CAIXA ALTA."""
    assert nome_amigavel("EXPRESSO ARAGUAIA LTDA") == "Araguaia"


def test_conectivos_ficam_em_minusculo() -> None:
    assert nome_amigavel("VIACAO SAO JOAO DE DEUS LTDA") == "Viacao Sao Joao de Deus"


def test_primeira_palavra_capitalizada_mesmo_sendo_conectivo() -> None:
    assert nome_amigavel("DE PAULA TRANSPORTES") == "De Paula"


def test_razao_social_so_de_sufixos_preserva_o_original() -> None:
    """Limpar demais seria pior que nao limpar: o campo ficaria vazio."""
    assert nome_amigavel("TRANSPORTES LTDA") == "Transportes Ltda"


@pytest.mark.parametrize("entrada", [None, "", "   "])
def test_ausente_continua_ausente(entrada: str | None) -> None:
    """A UI trata `None` como campo omitido; string vazia viraria linha em branco."""
    assert nome_amigavel(entrada) is None


def test_nunca_devolve_string_vazia() -> None:
    """Uma transportadora com nome estranho nao pode sumir da tela."""
    for bruto in ["S.A", "LTDA", "- - -", "..."]:
        resultado = nome_amigavel(bruto)
        assert resultado is None or resultado.strip()
