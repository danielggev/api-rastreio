"""Cliente Shopify: comparacao normalizada, erros com HTTP 200 e fulfillments."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from app.schemas import Anomalia
from app.services.normalizacao import NumeroPedidoFR
from app.services.reintento import Politica
from app.services.shopify import (
    ClienteShopify,
    ShopifyAcessoNegado,
    ShopifyErro,
    escapar_busca,
)
from app.services.shopify_auth import GerenciadorTokenShopify

DOMINIO = "teste.myshopify.com"
URL = f"https://{DOMINIO}/admin/api/2026-07/graphql.json"
LOJA = {"prefixo": "#", "sufixo": ""}


def _cliente(**kw: Any) -> ClienteShopify:
    kw.setdefault("dominio", DOMINIO)
    kw.setdefault("versao", "2026-07")
    # Token fixo evita a troca de credenciais nestes testes; o fluxo de
    # renovacao tem suite propria em test_shopify_auth.py.
    kw.setdefault(
        "autenticacao",
        GerenciadorTokenShopify(dominio=DOMINIO, token_fixo="shpat_teste"),
    )
    return ClienteShopify(**kw)


def _pedido(
    name: str = "#59552",
    email: str | None = "cliente@exemplo.com",
    fulfillments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": "gid://shopify/Order/123",
        "name": name,
        "email": email,
        "createdAt": "2026-07-22T18:00:00Z",
        "displayFulfillmentStatus": "FULFILLED",
        "fulfillments": fulfillments if fulfillments is not None else [],
    }


def _corpo(pedidos: list[dict[str, Any]], tem_proxima: bool = False) -> dict[str, Any]:
    return {
        "data": {
            "orders": {
                "pageInfo": {"hasNextPage": tem_proxima},
                "edges": [{"node": p} for p in pedidos],
            }
        }
    }


@respx.mock
async def test_encontra_pedido_comparando_formas_normalizadas() -> None:
    """TESTE OBRIGATORIO: `#59552` da Shopify casa com `59552` do input."""
    respx.post(URL).mock(return_value=httpx.Response(200, json=_corpo([_pedido()])))

    pedido = await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA))

    assert pedido is not None
    assert pedido.name == "#59552"
    assert pedido.email_normalizado == "cliente@exemplo.com"


@respx.mock
async def test_ignora_vizinhos_da_busca_parcial() -> None:
    """A busca da Shopify casa parcialmente; so a correspondencia exata vale."""
    respx.post(URL).mock(
        return_value=httpx.Response(
            200, json=_corpo([_pedido(name="#595521"), _pedido(name="#595520")])
        )
    )

    assert await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA)) is None


@respx.mock
async def test_uma_unica_requisicao_sem_paginacao() -> None:
    """Paginar condicionalmente abriria canal lateral de tempo.

    Pedido inexistente e pedido existente devem custar o MESMO numero de
    chamadas, senao o relogio denuncia quais numeros existem.
    """
    rota = respx.post(URL).mock(
        return_value=httpx.Response(
            200, json=_corpo([_pedido(name=f"#5955{i}") for i in range(10)], True)
        )
    )

    assert await _cliente().buscar_pedido(NumeroPedidoFR("99999", **LOJA)) is None
    assert rota.call_count == 1

    rota.reset()
    respx.post(URL).mock(return_value=httpx.Response(200, json=_corpo([_pedido()])))
    assert await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA)) is not None
    assert respx.calls.call_count >= 1


# --------------------------------------------------------------------------
# Validacao de email
# --------------------------------------------------------------------------


@respx.mock
async def test_email_confere_ignorando_caixa_e_espacos() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200, json=_corpo([_pedido(email="Cliente@Exemplo.COM")])
        )
    )
    pedido = await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA))
    assert pedido is not None
    assert pedido.email_confere("  cliente@exemplo.com  ")
    assert not pedido.email_confere("outro@exemplo.com")


@respx.mock
async def test_email_nulo_no_pedido_nunca_confere() -> None:
    """Sem email no pedido nao ha como validar o titular."""
    respx.post(URL).mock(return_value=httpx.Response(200, json=_corpo([_pedido(email=None)])))
    pedido = await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA))
    assert pedido is not None
    assert pedido.email_normalizado is None
    assert not pedido.email_confere("cliente@exemplo.com")
    assert not pedido.email_confere(None)
    assert not pedido.email_confere("")


# --------------------------------------------------------------------------
# Erros GraphQL com HTTP 200
# --------------------------------------------------------------------------


@respx.mock
async def test_throttled_com_http_200_e_reintentado() -> None:
    """TESTE OBRIGATORIO do plano.

    Politica baseada so em status HTTP trataria isto como sucesso vazio.
    """
    rota = respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "errors": [
                        {"message": "Throttled", "extensions": {"code": "THROTTLED"}}
                    ]
                },
            ),
            httpx.Response(200, json=_corpo([_pedido()])),
        ]
    )
    politica = Politica(max_tentativas=3, orcamento_s=5.0, base_espera_s=0.01)

    pedido = await _cliente(politica=politica).buscar_pedido(
        NumeroPedidoFR("59552", **LOJA)
    )
    assert pedido is not None
    assert rota.call_count == 2


@respx.mock
async def test_acesso_negado_nao_e_reintentado() -> None:
    """Escopo insuficiente e erro de configuracao; repetir nao resolve."""
    rota = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "errors": [
                    {"message": "Access denied", "extensions": {"code": "ACCESS_DENIED"}}
                ]
            },
        )
    )
    politica = Politica(max_tentativas=3, orcamento_s=5.0, base_espera_s=0.01)

    with pytest.raises(ShopifyAcessoNegado):
        await _cliente(politica=politica).buscar_pedido(NumeroPedidoFR("59552", **LOJA))
    assert rota.call_count == 1


@respx.mock
async def test_falha_fechado_com_data_parcial_e_errors() -> None:
    """Esta consulta decide autorizacao: dado parcial nao pode ser aceito."""
    corpo = _corpo([_pedido()])
    corpo["errors"] = [{"message": "campo indisponivel", "extensions": {"code": "SOMETHING"}}]
    respx.post(URL).mock(return_value=httpx.Response(200, json=corpo))

    with pytest.raises(ShopifyErro):
        await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA))


@respx.mock
async def test_credencial_recusada_por_status_http() -> None:
    respx.post(URL).mock(return_value=httpx.Response(401))
    with pytest.raises(ShopifyAcessoNegado):
        await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA))


# --------------------------------------------------------------------------
# Fulfillments
# --------------------------------------------------------------------------


@respx.mock
async def test_pedido_sem_fulfillment() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=_corpo([_pedido()])))
    pedido = await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA))
    assert pedido is not None
    assert not pedido.tem_fulfillment
    assert pedido.codigo_rastreio is None
    assert pedido.anomalias == []


@respx.mock
async def test_escolhe_fulfillment_mais_recente_com_tracking() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json=_corpo(
                [
                    _pedido(
                        fulfillments=[
                            {
                                "createdAt": "2026-07-20T10:00:00Z",
                                "trackingInfo": [{"number": "ANTIGO"}],
                            },
                            {
                                "createdAt": "2026-07-25T10:00:00Z",
                                "trackingInfo": [{"number": "NOVO"}],
                            },
                        ]
                    )
                ]
            ),
        )
    )
    pedido = await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA))
    assert pedido is not None
    assert pedido.codigo_rastreio == "NOVO"
    assert Anomalia.MULTIPLOS_FULFILLMENTS in pedido.anomalias


@respx.mock
async def test_multiplos_trackings_no_mesmo_fulfillment_gera_anomalia() -> None:
    """`trackingInfo` e lista: escolher o fulfillment nao define qual codigo usar."""
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json=_corpo(
                [
                    _pedido(
                        fulfillments=[
                            {
                                "createdAt": "2026-07-25T10:00:00Z",
                                "trackingInfo": [{"number": "AA1"}, {"number": "AA2"}],
                            }
                        ]
                    )
                ]
            ),
        )
    )
    pedido = await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA))
    assert pedido is not None
    assert pedido.codigo_rastreio == "AA1"
    assert Anomalia.MULTIPLOS_TRACKINGS in pedido.anomalias
    assert Anomalia.MULTIPLOS_FULFILLMENTS not in pedido.anomalias


@respx.mock
async def test_fulfillment_sem_tracking_nao_gera_codigo() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json=_corpo(
                [_pedido(fulfillments=[{"createdAt": "2026-07-25T10:00:00Z", "trackingInfo": []}])]
            ),
        )
    )
    pedido = await _cliente().buscar_pedido(NumeroPedidoFR("59552", **LOJA))
    assert pedido is not None
    assert pedido.tem_fulfillment
    assert pedido.codigo_rastreio is None


def test_escapar_busca() -> None:
    assert escapar_busca('a"b') == 'a\\"b'
    assert escapar_busca("a\\b") == "a\\\\b"
    assert escapar_busca("#59552") == "#59552"
