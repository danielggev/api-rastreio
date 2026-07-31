"""Obtencao e renovacao do access token da Shopify.

A partir de 1o de janeiro de 2026 a Shopify deixou de permitir a criacao de apps
personalizados legados -- aqueles que entregavam um token `shpat_` fixo, copiado
uma vez e usado para sempre. Apps criados no Dev Dashboard usam o
**client credentials grant**: a aplicacao troca `client_id` + `client_secret` por
um access token que **expira em 24 horas** e precisa ser renovado.

    POST https://{loja}/admin/oauth/access_token
    grant_type=client_credentials&client_id=...&client_secret=...

Requisitos: o app precisa estar INSTALADO na loja e pertencer a MESMA
organizacao. Caso contrario a Shopify responde `shop_not_permitted`.

Apps legados que ainda existem (como o do ERP) continuam funcionando com token
fixo -- por isso `token_fixo` segue suportado.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from app.config import get_settings
from app.services.logs import redigir_excecao
from app.services.reintento import Permanente, Politica, Transitorio, com_reintento

logger = logging.getLogger(__name__)

_CAMINHO = "/admin/oauth/access_token"

# Renova com folga: um token que expira no meio de uma requisicao produziria um
# 401 dificil de diagnosticar.
MARGEM_RENOVACAO_S = 300


class ShopifyAutenticacaoErro(Exception):
    """Falha ao obter o access token."""


# A Shopify responde erros de OAuth com uma PAGINA HTML, nao com JSON. Sem
# extrair o codigo dali, o diagnostico vira "HTTP 400" -- que nao diz nada a
# quem precisa corrigir.
_ERRO_HTML = re.compile(r"(?i)oauth error\s+([a-z_]+)")

_EXPLICACOES = {
    "app_not_installed": (
        "o app nao esta instalado nesta loja. No Dev Dashboard, configure a "
        "distribuicao do app para esta loja e instale-o."
    ),
    "shop_not_permitted": (
        "app e loja estao em organizacoes diferentes. O client credentials so "
        "funciona quando ambos pertencem a mesma organizacao."
    ),
    "invalid_client": (
        "client_id ou client_secret incorretos. Confira em "
        "Dev Dashboard > seu app > Configuracoes > Credenciais."
    ),
    "invalid_request": "requisicao malformada na troca de credenciais.",
}


def _extrair_erro_oauth(resposta: httpx.Response) -> str:
    """Codigo de erro legivel a partir de JSON ou da pagina HTML de erro."""
    try:
        corpo = resposta.json()
        if isinstance(corpo, dict):
            codigo = str(corpo.get("error") or corpo.get("errors") or "").strip()
            if codigo:
                return f"{codigo} -- {_EXPLICACOES.get(codigo, 'ver documentacao')}"
    except ValueError:
        pass

    achado = _ERRO_HTML.search(resposta.text or "")
    if achado:
        codigo = achado.group(1).lower()
        return f"{codigo} -- {_EXPLICACOES.get(codigo, 'ver documentacao')}"

    return "sem detalhe na resposta"


class GerenciadorTokenShopify:
    def __init__(
        self,
        *,
        dominio: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_fixo: str | None = None,
        cliente_http: httpx.AsyncClient | None = None,
        politica: Politica | None = None,
        margem_s: int = MARGEM_RENOVACAO_S,
    ) -> None:
        s = get_settings()
        self._dominio = dominio or s.shopify_shop_domain
        self._client_id = client_id if client_id is not None else s.shopify_client_id
        self._client_secret = (
            client_secret if client_secret is not None else s.shopify_client_secret
        )
        self._token_fixo = (
            token_fixo if token_fixo is not None else s.shopify_access_token
        )
        self._http = cliente_http
        self._politica = politica or Politica()
        self._margem = margem_s

        self._token: str | None = None
        self._expira_em: float = 0.0
        # Evita que varias requisicoes simultaneas disparem trocas em paralelo.
        self._trava = asyncio.Lock()

    @property
    def url(self) -> str:
        return f"https://{self._dominio}{_CAMINHO}"

    @property
    def usa_token_fixo(self) -> bool:
        return bool(self._token_fixo)

    async def obter(self) -> str:
        """Access token valido, renovando quando necessario."""
        if self._token_fixo:
            return self._token_fixo

        if not (self._client_id and self._client_secret):
            raise ShopifyAutenticacaoErro(
                "sem credenciais: defina SHOPIFY_CLIENT_ID e SHOPIFY_CLIENT_SECRET "
                "(app do Dev Dashboard) ou SHOPIFY_ACCESS_TOKEN (app legado)"
            )

        if self._valido():
            assert self._token is not None
            return self._token

        async with self._trava:
            # Outra corrotina pode ter renovado enquanto esperavamos a trava.
            if self._valido():
                assert self._token is not None
                return self._token
            return await self._renovar()

    def _valido(self) -> bool:
        return self._token is not None and time.monotonic() < self._expira_em

    async def _renovar(self) -> str:
        async def chamar(timeout: float) -> dict[str, Any]:
            return await self._trocar_credenciais(timeout)

        try:
            corpo = await com_reintento(
                chamar, self._politica, nome="Shopify token exchange"
            )
        except (Transitorio, Permanente) as exc:
            raise ShopifyAutenticacaoErro(redigir_excecao(exc)) from None

        token = corpo.get("access_token")
        if not isinstance(token, str) or not token:
            raise ShopifyAutenticacaoErro("resposta sem access_token")

        expira_em_s = corpo.get("expires_in")
        vida = int(expira_em_s) if isinstance(expira_em_s, int | float) else 86399

        # Nunca deixa a margem consumir a validade inteira em tokens curtos.
        efetiva = max(vida - self._margem, vida // 2, 1)
        self._token = token
        self._expira_em = time.monotonic() + efetiva

        logger.info(
            "access token da Shopify renovado (validade %ds, renova em %ds)",
            vida,
            efetiva,
        )
        return token

    async def _trocar_credenciais(self, timeout: float) -> dict[str, Any]:
        dados = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }

        try:
            if self._http is not None:
                resposta = await self._http.post(self.url, data=dados, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as http:
                    resposta = await http.post(self.url, data=dados)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            raise Transitorio(redigir_excecao(exc)) from None
        except httpx.HTTPError as exc:
            raise Permanente(redigir_excecao(exc)) from None

        if resposta.status_code == 429 or resposta.status_code >= 500:
            raise Transitorio(
                f"troca de credenciais devolveu HTTP {resposta.status_code}"
            )
        if resposta.status_code >= 400:
            # 400/401 aqui e quase sempre configuracao: credencial errada, app nao
            # instalado, ou app e loja em organizacoes diferentes. Repetir nao
            # resolve -- o que resolve e uma mensagem que diga o que fazer.
            raise Permanente(
                f"troca de credenciais recusada (HTTP {resposta.status_code}): "
                f"{_extrair_erro_oauth(resposta)}"
            )

        try:
            corpo: Any = resposta.json()
        except ValueError:
            raise Permanente("resposta da troca de credenciais nao e JSON") from None

        if not isinstance(corpo, dict):
            raise Permanente("resposta da troca de credenciais nao e um objeto")

        return corpo
