"""Obtencao e renovacao do access token da Shopify (client credentials grant).

Apps do Dev Dashboard nao entregam token fixo: a aplicacao troca client_id +
client_secret por um token que expira em 24 horas.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.services import shopify_auth
from app.services.reintento import Politica
from app.services.shopify_auth import (
    GerenciadorTokenShopify,
    ShopifyAutenticacaoErro,
)

DOMINIO = "teste.myshopify.com"
URL = f"https://{DOMINIO}/admin/oauth/access_token"
# Valores ficticios: credencial real nao entra em arquivo versionado, nem
# mesmo o client_id -- ele identifica o app da loja sem necessidade nenhuma.
CLIENT_ID = "00000000000000000000000000000000"
CLIENT_SECRET = "shpss_segredoficticiodetestes"

POLITICA = Politica(max_tentativas=3, orcamento_s=5.0, base_espera_s=0.01)


def _gerenciador(**kw: object) -> GerenciadorTokenShopify:
    kw.setdefault("dominio", DOMINIO)
    kw.setdefault("client_id", CLIENT_ID)
    kw.setdefault("client_secret", CLIENT_SECRET)
    kw.setdefault("token_fixo", "")
    kw.setdefault("politica", POLITICA)
    return GerenciadorTokenShopify(**kw)  # type: ignore[arg-type]


def _resposta(token: str = "shpat_gerado", expires_in: int = 86399) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": token,
            "scope": "read_orders",
            "expires_in": expires_in,
        },
    )


# --------------------------------------------------------------------------
# Caminho feliz
# --------------------------------------------------------------------------


@respx.mock
async def test_troca_credenciais_por_token() -> None:
    rota = respx.post(URL).mock(return_value=_resposta())

    assert await _gerenciador().obter() == "shpat_gerado"

    corpo = rota.calls[0].request.content.decode()
    assert "grant_type=client_credentials" in corpo
    assert f"client_id={CLIENT_ID}" in corpo


@respx.mock
async def test_token_e_reaproveitado_enquanto_valido() -> None:
    """Trocar credenciais a cada requisicao gastaria o limite da Shopify a toa."""
    rota = respx.post(URL).mock(return_value=_resposta())
    g = _gerenciador()

    for _ in range(5):
        assert await g.obter() == "shpat_gerado"

    assert rota.call_count == 1


@respx.mock
async def test_renova_quando_o_token_expira(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relogio controlado: o teste nao pode depender de esperar 24 horas."""
    rota = respx.post(URL).mock(
        side_effect=[_resposta("token-1", 3600), _resposta("token-2", 3600)]
    )
    relogio = {"agora": 1000.0}
    monkeypatch.setattr(
        shopify_auth.time, "monotonic", lambda: relogio["agora"]
    )
    g = _gerenciador(margem_s=300)

    assert await g.obter() == "token-1"

    # Avanca alem da validade menos a margem.
    relogio["agora"] += 3400
    assert await g.obter() == "token-2"
    assert rota.call_count == 2


@respx.mock
async def test_renova_antes_de_expirar_de_fato(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A margem existe para o token nunca vencer no meio de uma requisicao."""
    rota = respx.post(URL).mock(
        side_effect=[_resposta("token-1", 3600), _resposta("token-2", 3600)]
    )
    relogio = {"agora": 1000.0}
    monkeypatch.setattr(
        shopify_auth.time, "monotonic", lambda: relogio["agora"]
    )
    g = _gerenciador(margem_s=300)

    assert await g.obter() == "token-1"

    # Faltam 200s para expirar: dentro da margem de 300s, ja renova.
    relogio["agora"] += 3400
    assert await g.obter() == "token-2"
    assert rota.call_count == 2


@respx.mock
async def test_margem_nunca_zera_a_validade_de_token_curto() -> None:
    """Com margem maior que a vida do token, ainda assim ele vale por um tempo.

    Sem esse piso, um token curto seria considerado expirado no mesmo instante
    em que chegou, gerando renovacao em toda requisicao.
    """
    rota = respx.post(URL).mock(return_value=_resposta("token-curto", 100))
    g = _gerenciador(margem_s=10_000)

    assert await g.obter() == "token-curto"
    assert await g.obter() == "token-curto"
    assert rota.call_count == 1


@respx.mock
async def test_requisicoes_simultaneas_trocam_credenciais_uma_unica_vez() -> None:
    """Sem a trava, uma rajada dispararia varias trocas em paralelo."""
    rota = respx.post(URL).mock(return_value=_resposta())
    g = _gerenciador()

    tokens = await asyncio.gather(*(g.obter() for _ in range(10)))

    assert set(tokens) == {"shpat_gerado"}
    assert rota.call_count == 1


# --------------------------------------------------------------------------
# App legado
# --------------------------------------------------------------------------


@respx.mock
async def test_token_fixo_dispensa_a_troca() -> None:
    """Apps personalizados legados continuam funcionando sem client credentials."""
    rota = respx.post(URL).mock(return_value=_resposta())
    g = _gerenciador(token_fixo="shpat_legado")

    assert await g.obter() == "shpat_legado"
    assert rota.call_count == 0
    assert g.usa_token_fixo


# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------


async def test_sem_credenciais_falha_com_mensagem_util() -> None:
    g = _gerenciador(client_id="", client_secret="")
    with pytest.raises(ShopifyAutenticacaoErro, match="SHOPIFY_CLIENT_ID"):
        await g.obter()


@respx.mock
async def test_credencial_recusada_nao_e_reintentada() -> None:
    """400/401 aqui e configuracao (app nao instalado, org diferente)."""
    rota = respx.post(URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )

    with pytest.raises(ShopifyAutenticacaoErro) as info:
        await _gerenciador().obter()

    assert rota.call_count == 1
    assert "invalid_client" in str(info.value)
    assert "client_secret" in str(info.value)


@respx.mock
async def test_erro_oauth_em_html_e_traduzido() -> None:
    """A Shopify responde erro de OAuth com PAGINA HTML, nao JSON.

    Sem extrair o codigo dali, o diagnostico vira "HTTP 400" -- inutil para quem
    precisa corrigir. Este caso e real: foi o que aconteceu na primeira
    validacao contra a loja.
    """
    html = (
        "<!DOCTYPE html><html><body><h1>400 - Oauth error app_not_installed</h1>"
        "<p>Oauth error app_not_installed: The application is not installed on "
        "this shop.</p></body></html>"
    )
    respx.post(URL).mock(
        return_value=httpx.Response(400, html=html, headers={"content-type": "text/html"})
    )

    with pytest.raises(ShopifyAutenticacaoErro) as info:
        await _gerenciador().obter()

    mensagem = str(info.value)
    assert "app_not_installed" in mensagem
    assert "instalado nesta loja" in mensagem


@respx.mock
async def test_erro_oauth_desconhecido_nao_quebra() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            400, html="<html>Oauth error algum_erro_novo</html>"
        )
    )
    with pytest.raises(ShopifyAutenticacaoErro, match="algum_erro_novo"):
        await _gerenciador().obter()


@respx.mock
async def test_falha_transitoria_e_reintentada() -> None:
    rota = respx.post(URL).mock(
        side_effect=[httpx.Response(503), _resposta("token-apos-retry")]
    )
    assert await _gerenciador().obter() == "token-apos-retry"
    assert rota.call_count == 2


@respx.mock
async def test_resposta_sem_access_token() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"scope": "read_orders"}))
    with pytest.raises(ShopifyAutenticacaoErro, match="sem access_token"):
        await _gerenciador().obter()


@respx.mock
async def test_client_secret_nunca_aparece_em_excecao() -> None:
    """O secret vai no corpo da requisicao; excecoes do httpx podem carrega-lo."""
    respx.post(URL).mock(
        side_effect=httpx.ConnectError(f"falha enviando client_secret={CLIENT_SECRET}")
    )

    with pytest.raises(ShopifyAutenticacaoErro) as info:
        await _gerenciador().obter()

    assert CLIENT_SECRET not in str(info.value)
