"""Cliente da Frete Rapido: guardas, reintento e redacao de segredo."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
import respx

from app.services.frete_rapido import ClienteFreteRapido, FreteRapidoErro
from app.services.logs import redigir, redigir_excecao
from app.services.normalizacao import NumeroPedidoFR
from app.services.reintento import Politica

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://freterapido.com"
URL = f"{BASE}/api/external/embarcador/v1/quotes/59552/occurrences"
TOKEN = "t" * 32

LOJA = {"prefixo": "#", "sufixo": ""}


def _cliente(**kw: object) -> ClienteFreteRapido:
    return ClienteFreteRapido(base_url=BASE, **kw)  # type: ignore[arg-type]


def _payload(nome: str) -> list[dict[str, object]]:
    return json.loads((FIXTURES / nome).read_text(encoding="utf-8"))


@respx.mock
async def test_busca_ocorrencias_com_payload_real() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=_payload("resposta-59552.json")))

    ocorrencias = await _cliente().buscar_ocorrencias(NumeroPedidoFR("#59552", **LOJA), TOKEN)

    assert [o.codigo for o in ocorrencias] == [0, 1]
    assert ocorrencias[0].nome == "Contratado"
    assert ocorrencias[0].razao_social_transportadora == "JADLOG LOGISTICA S.A"
    assert [o.indice_origem for o in ocorrencias] == [0, 1]


@respx.mock
async def test_lista_de_permissao_descarta_dado_pessoal_de_terceiro() -> None:
    """TESTE OBRIGATORIO do plano.

    O payload pode trazer CPF/CNPJ e nome do entregador -- dados pessoais de
    terceiros, sem utilidade para a resposta. Nao podem sobreviver ao parsing.
    """
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "codigo": 3,
                    "nome": "Entregue",
                    "data_ocorrencia": "2026-07-28 10:00:00",
                    "nome_entregador": "Fulano de Tal",
                    "cnpj_cpf_entregador": "123.456.789-00",
                    "comprovantes": [{"url": "https://exemplo/comprovante.jpg"}],
                }
            ],
        )
    )

    ocorrencias = await _cliente().buscar_ocorrencias(NumeroPedidoFR("59552", **LOJA), TOKEN)

    serializada = ocorrencias[0].model_dump()
    assert "cnpj_cpf_entregador" not in serializada
    assert "nome_entregador" not in serializada
    assert "comprovantes" not in serializada
    assert "123.456.789-00" not in json.dumps(serializada, default=str)


@respx.mock
async def test_codigo_aceita_inteiro_e_string_numerica() -> None:
    """As fixtures trazem inteiro; a documentacao mostra string."""
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"codigo": 0, "nome": "Contratado"},
                {"codigo": "1", "nome": "Aguardando coleta / postagem"},
            ],
        )
    )

    ocorrencias = await _cliente().buscar_ocorrencias(NumeroPedidoFR("59552", **LOJA), TOKEN)
    assert [o.codigo for o in ocorrencias] == [0, 1]
    assert all(isinstance(o.codigo, int) for o in ocorrencias)


async def test_guarda_recusa_numero_com_cerquilha() -> None:
    """TESTE OBRIGATORIO do plano.

    Um "#" que escape faz toda consulta voltar vazia, sem erro. A guarda
    transforma o silencio em excecao alta.
    """
    with pytest.raises(FreteRapidoErro, match="nao normalizado"):
        await _cliente().buscar_ocorrencias("#59552", TOKEN)  # type: ignore[arg-type]


async def test_guarda_recusa_str_crua() -> None:
    with pytest.raises(FreteRapidoErro, match="nao normalizado"):
        await _cliente().buscar_ocorrencias("59552", TOKEN)  # type: ignore[arg-type]


@respx.mock
async def test_ocorrencia_malformada_nao_derruba_o_historico() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"codigo": 0, "nome": "Contratado"},
                {"codigo": "nao-numerico", "nome": "Lixo"},
                {"codigo": 2, "nome": "Em transito"},
            ],
        )
    )

    ocorrencias = await _cliente().buscar_ocorrencias(NumeroPedidoFR("59552", **LOJA), TOKEN)
    assert [o.codigo for o in ocorrencias] == [0, 2]


@respx.mock
async def test_lista_vazia_e_resultado_valido() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=[]))
    assert await _cliente().buscar_ocorrencias(NumeroPedidoFR("59552", **LOJA), TOKEN) == []


@respx.mock
async def test_reintenta_apenas_falha_transitoria() -> None:
    rota = respx.get(URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=_payload("resposta-59552.json")),
        ]
    )
    politica = Politica(max_tentativas=3, orcamento_s=5.0, base_espera_s=0.01)

    ocorrencias = await _cliente(politica=politica).buscar_ocorrencias(
        NumeroPedidoFR("59552", **LOJA), TOKEN
    )
    assert len(ocorrencias) == 2
    assert rota.call_count == 2


@respx.mock
async def test_nao_reintenta_erro_permanente() -> None:
    """Repetir um 404 e inutil e so amplifica carga."""
    rota = respx.get(URL).mock(return_value=httpx.Response(404))
    politica = Politica(max_tentativas=3, orcamento_s=5.0, base_espera_s=0.01)

    with pytest.raises(FreteRapidoErro):
        await _cliente(politica=politica).buscar_ocorrencias(
            NumeroPedidoFR("59552", **LOJA), TOKEN
        )
    assert rota.call_count == 1


@respx.mock
async def test_reintento_respeita_o_teto_de_tentativas() -> None:
    rota = respx.get(URL).mock(return_value=httpx.Response(500))
    politica = Politica(max_tentativas=3, orcamento_s=5.0, base_espera_s=0.01)

    with pytest.raises(FreteRapidoErro):
        await _cliente(politica=politica).buscar_ocorrencias(
            NumeroPedidoFR("59552", **LOJA), TOKEN
        )
    assert rota.call_count == 3


# --------------------------------------------------------------------------
# Redacao de segredo
# --------------------------------------------------------------------------


def test_redigir_remove_token_da_query_string() -> None:
    url = f"{URL}?token={TOKEN}&codigo_volume=AA1"
    saida = redigir(url)
    assert TOKEN not in saida
    assert "token=***" in saida
    assert "codigo_volume=AA1" in saida


@pytest.mark.parametrize(
    "credencial",
    [
        "shpat_segredoxyz123",  # Admin API access token
        "shpss_segredoxyz123",  # API secret key
        "shpca_segredoxyz123",
        "shppa_segredoxyz123",
    ],
)
def test_redigir_remove_credencial_shopify(credencial: str) -> None:
    """Cobrir a familia inteira de prefixos, nao apenas o access token."""
    saida = redigir(f"falha ao autenticar com {credencial}")
    assert credencial not in saida
    assert "***" in saida


def test_redigir_excecao_nao_vaza_url_completa() -> None:
    exc = httpx.ConnectError(f"falha ao conectar em {URL}?token={TOKEN}")
    assert TOKEN not in redigir_excecao(exc)


@respx.mock
async def test_token_nunca_aparece_em_log_nem_em_excecao(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TESTE OBRIGATORIO do plano.

    O token vai na query string, e excecoes do httpx incluem a URL. Sem redacao,
    um caminho de erro grava o segredo em texto puro.
    """
    respx.get(URL).mock(side_effect=httpx.ConnectError(f"erro em {URL}?token={TOKEN}"))
    politica = Politica(max_tentativas=2, orcamento_s=5.0, base_espera_s=0.01)

    with caplog.at_level(logging.DEBUG), pytest.raises(FreteRapidoErro) as info:
        await _cliente(politica=politica).buscar_ocorrencias(
            NumeroPedidoFR("59552", **LOJA), TOKEN
        )

    assert TOKEN not in str(info.value)
    assert TOKEN not in caplog.text
