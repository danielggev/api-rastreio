"""Selecao de token entre os 3 CNPJs.

O caso mais perigoso e o pedido marcado com a tag errada: confiar cegamente nela
responderia "nao despachado" para um pedido que existe em outro CNPJ.
"""

from __future__ import annotations

import pytest

from app.schemas import Anomalia, OcorrenciaFR
from app.services.frete_rapido import FreteRapidoErro
from app.services.multi_cnpj import BuscadorMultiCNPJ
from app.services.normalizacao import NumeroPedidoFR

LOJA = {"prefixo": "#", "sufixo": ""}
NUMERO = NumeroPedidoFR("59552", **LOJA)

TOKENS = {
    "empresa-a": "a" * 32,
    "empresa-b": "b" * 32,
    "empresa-c": "c" * 32,
}


class ClienteFalso:
    """Devolve ocorrencias apenas para os tokens que "conhecem" o pedido."""

    def __init__(
        self,
        dados_por_token: dict[str, list[OcorrenciaFR]] | None = None,
        falhas: set[str] | None = None,
    ) -> None:
        self.dados = dados_por_token or {}
        self.falhas = falhas or set()
        self.chamadas: list[str] = []

    async def buscar_ocorrencias(
        self, numero: NumeroPedidoFR, token: str
    ) -> list[OcorrenciaFR]:
        self.chamadas.append(token)
        if token in self.falhas:
            raise FreteRapidoErro("indisponivel")
        return list(self.dados.get(token, []))


def _ocorrencias() -> list[OcorrenciaFR]:
    return [OcorrenciaFR(codigo=2, nome="Em transito")]


def _buscador(cliente: ClienteFalso, tokens: dict[str, str] | None = None) -> BuscadorMultiCNPJ:
    return BuscadorMultiCNPJ(cliente, tokens or TOKENS)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Caminho rapido: a tag resolve
# --------------------------------------------------------------------------


async def test_tag_conhecida_consulta_apenas_um_token() -> None:
    cliente = ClienteFalso({TOKENS["empresa-b"]: _ocorrencias()})
    resultado = await _buscador(cliente).buscar(NUMERO, ["empresa-b"])

    assert resultado.ocorrencias
    assert resultado.cnpj == "empresa-b"
    assert cliente.chamadas == [TOKENS["empresa-b"]]
    assert resultado.anomalias == []


async def test_tag_casa_ignorando_caixa_e_espacos() -> None:
    cliente = ClienteFalso({TOKENS["empresa-a"]: _ocorrencias()})
    resultado = await _buscador(cliente).buscar(NUMERO, ["empresa-a"])
    assert resultado.cnpj == "empresa-a"


async def test_ignora_tags_que_nao_sao_de_cnpj() -> None:
    """Pedidos tem varias tags; so uma delas identifica a empresa."""
    cliente = ClienteFalso({TOKENS["empresa-c"]: _ocorrencias()})
    resultado = await _buscador(cliente).buscar(
        NUMERO, ["black-friday", "presente", "empresa-c", "frete-gratis"]
    )
    assert resultado.cnpj == "empresa-c"
    assert cliente.chamadas == [TOKENS["empresa-c"]]


# --------------------------------------------------------------------------
# Caminho seguro
# --------------------------------------------------------------------------


async def test_pedido_sem_tag_consulta_todos_os_cnpjs() -> None:
    cliente = ClienteFalso({TOKENS["empresa-c"]: _ocorrencias()})
    resultado = await _buscador(cliente).buscar(NUMERO, [])

    assert resultado.cnpj == "empresa-c"
    assert len(cliente.chamadas) == 3
    assert Anomalia.TAG_CNPJ_AUSENTE in resultado.anomalias


async def test_tag_desconhecida_consulta_todos_e_registra_anomalia() -> None:
    """Tag renomeada ou CNPJ novo nao configurado."""
    cliente = ClienteFalso({TOKENS["empresa-a"]: _ocorrencias()})
    resultado = await _buscador(cliente).buscar(NUMERO, ["empresa-nova"])

    assert resultado.cnpj == "empresa-a"
    assert Anomalia.TAG_CNPJ_DESCONHECIDA in resultado.anomalias


async def test_tag_errada_encontra_o_pedido_no_cnpj_correto() -> None:
    """O caso perigoso: pedido marcado como A, mas os dados estao em B.

    Confiar cegamente na tag responderia "nao despachado" para um pedido que
    existe.
    """
    cliente = ClienteFalso({TOKENS["empresa-b"]: _ocorrencias()})
    resultado = await _buscador(cliente).buscar(NUMERO, ["empresa-a"])

    assert resultado.ocorrencias
    assert resultado.cnpj == "empresa-b"
    assert Anomalia.CNPJ_DIVERGENTE_DA_TAG in resultado.anomalias
    # Tentou o da tag primeiro, depois os outros dois.
    assert cliente.chamadas[0] == TOKENS["empresa-a"]
    assert len(cliente.chamadas) == 3


async def test_vazio_em_todos_os_cnpjs_nao_e_falha() -> None:
    """Pedido realmente sem ocorrencias: resultado vazio, sem erro."""
    cliente = ClienteFalso({})
    resultado = await _buscador(cliente).buscar(NUMERO, ["empresa-a"])

    assert resultado.ocorrencias == []
    assert not resultado.houve_falha
    assert resultado.cnpj is None


# --------------------------------------------------------------------------
# Falha fechada
# --------------------------------------------------------------------------


async def test_falha_de_token_sem_dados_marca_houve_falha() -> None:
    """TESTE CRITICO.

    Se um CNPJ falhou e nenhum devolveu dados, NAO da para afirmar que o pedido
    nao foi despachado -- o token que falhou pode ser o que tinha as ocorrencias.
    O chamador precisa responder erro, nunca "sem rastreio".
    """
    cliente = ClienteFalso({}, falhas={TOKENS["empresa-b"]})
    resultado = await _buscador(cliente).buscar(NUMERO, [])

    assert resultado.ocorrencias == []
    assert resultado.houve_falha


async def test_falha_no_cnpj_da_tag_e_preservada_quando_ninguem_responde() -> None:
    cliente = ClienteFalso({}, falhas={TOKENS["empresa-a"]})
    resultado = await _buscador(cliente).buscar(NUMERO, ["empresa-a"])

    assert resultado.ocorrencias == []
    assert resultado.houve_falha


async def test_falha_de_um_token_nao_impede_sucesso_de_outro() -> None:
    cliente = ClienteFalso(
        {TOKENS["empresa-c"]: _ocorrencias()}, falhas={TOKENS["empresa-a"]}
    )
    resultado = await _buscador(cliente).buscar(NUMERO, [])

    assert resultado.ocorrencias
    assert resultado.cnpj == "empresa-c"


# --------------------------------------------------------------------------
# Casos de borda
# --------------------------------------------------------------------------


async def test_dados_em_mais_de_um_cnpj_gera_anomalia_e_escolha_estavel() -> None:
    """Nao deveria acontecer: o numero do pedido e unico na loja."""
    cliente = ClienteFalso(
        {TOKENS["empresa-b"]: _ocorrencias(), TOKENS["empresa-c"]: _ocorrencias()}
    )
    resultado = await _buscador(cliente).buscar(NUMERO, [])

    assert Anomalia.MULTIPLOS_CNPJS_COM_DADOS in resultado.anomalias
    # Ordem da configuracao, nao a de chegada das respostas paralelas.
    assert resultado.cnpj == "empresa-b"


async def test_operacao_de_cnpj_unico_nao_precisa_de_tag() -> None:
    cliente = ClienteFalso({"token-unico": _ocorrencias()})
    resultado = await _buscador(cliente, {"": "token-unico"}).buscar(NUMERO, [])

    assert resultado.ocorrencias
    assert cliente.chamadas == ["token-unico"]
    assert resultado.anomalias == []


def test_recusa_configuracao_sem_token() -> None:
    with pytest.raises(ValueError, match="nenhum token"):
        BuscadorMultiCNPJ(ClienteFalso(), {})  # type: ignore[arg-type]
