"""Cliente da Shopify Admin API (GraphQL).

A Shopify entra no fluxo com dois papeis: validar que o email confere com o
pedido, e fornecer o codigo de rastreio para exibicao (a Frete Rapido nao o
devolve de forma confiavel).

Tres armadilhas tratadas aqui:

1. Erros chegam com HTTP 200 num array `errors`. Uma politica de reintento
   baseada so em status HTTP nunca repetiria um THROTTLED -- a chamada pareceria
   bem-sucedida e devolveria dados vazios.
2. Nao paginamos. Paginar condicionalmente abriria um canal lateral de TEMPO:
   pedido existente com email errado responderia rapido (casa na 1a pagina) e
   pedido inexistente responderia devagar (percorre varias). A diferenca e
   mensuravel e permitiria enumerar quais pedidos existem, destruindo a protecao
   da resposta generica identica.
3. O `name` vem com prefixo (`#59552`); a comparacao e sempre entre formas
   normalizadas dos dois lados.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.config import get_settings
from app.schemas import Anomalia
from app.services.logs import redigir_excecao
from app.services.normalizacao import (
    NumeroPedidoFR,
    montar_name_shopify,
    normalizar_email,
)
from app.services.reintento import Permanente, Politica, Transitorio, com_reintento
from app.services.shopify_auth import GerenciadorTokenShopify, ShopifyAutenticacaoErro

logger = logging.getLogger(__name__)

# Tamanho da pagina unica. Nao ha segunda chamada -- ver nota sobre tempo acima.
TAMANHO_PAGINA = 10

CONSULTA = """
query BuscarPedido($query: String!, $primeiros: Int!) {
  orders(first: $primeiros, query: $query) {
    pageInfo { hasNextPage }
    edges { node {
      id
      name
      email
      createdAt
      displayFulfillmentStatus
      # Identifica qual dos 3 CNPJs despachou -- escolhe o token da Frete Rapido.
      tags
      fulfillments(first: 10) {
        createdAt
        trackingInfo { number url company }
      }
    }}
  }
}
"""

# Codigos que valem reintento; os demais falham de imediato.
_TRANSITORIOS = {"THROTTLED", "INTERNAL_SERVER_ERROR"}


class ShopifyErro(Exception):
    """Falha ao consultar a Shopify."""


class ShopifyAcessoNegado(ShopifyErro):
    """Escopo insuficiente ou token invalido. Nao adianta repetir: e configuracao."""


@dataclass
class PedidoShopify:
    id: str
    name: str
    email_normalizado: str | None
    criado_em: datetime | None
    tem_fulfillment: bool
    codigo_rastreio: str | None
    # Tags normalizadas (minusculas, sem espacos nas bordas). Uma delas indica
    # qual CNPJ despachou o pedido.
    tags: list[str] = field(default_factory=list)
    anomalias: list[Anomalia] = field(default_factory=list)

    def email_confere(self, informado: str | None) -> bool:
        """Comparacao entre formas canonicas.

        `Order.email` pode ser nulo. Nesse caso NAO ha como validar o titular, e
        a resposta correta e tratar como nao encontrado -- nunca deixar passar
        por coincidencia de valores vazios.
        """
        if self.email_normalizado is None:
            return False
        alvo = normalizar_email(informado)
        if alvo is None:
            return False
        return self.email_normalizado == alvo


def escapar_busca(valor: str) -> str:
    """Escapa o valor para interpolacao segura na string de busca da Shopify."""
    return valor.replace("\\", "\\\\").replace('"', '\\"')


class ClienteShopify:
    def __init__(
        self,
        *,
        cliente_http: httpx.AsyncClient | None = None,
        dominio: str | None = None,
        autenticacao: GerenciadorTokenShopify | None = None,
        versao: str | None = None,
        politica: Politica | None = None,
    ) -> None:
        s = get_settings()
        self._dominio = dominio or s.shopify_shop_domain
        # O token nao e mais fixo: apps do Dev Dashboard usam client credentials
        # e o token expira em 24h. O gerenciador cuida da renovacao.
        self._auth = autenticacao or GerenciadorTokenShopify(
            dominio=self._dominio, cliente_http=cliente_http
        )
        # Versao FIXADA explicitamente: versoes expiram em ~1 ano.
        self._versao = versao or s.shopify_api_version
        self._http = cliente_http
        self._politica = politica or Politica()

    @property
    def url(self) -> str:
        return f"https://{self._dominio}/admin/api/{self._versao}/graphql.json"

    async def buscar_pedido(self, numero: NumeroPedidoFR) -> PedidoShopify | None:
        """Pedido cujo `name` normalizado bate exatamente com `numero`.

        Devolve `None` quando nao ha correspondencia exata. Quem chama nao pode
        distinguir "nao existe" de "email errado" na resposta ao cliente.
        """
        name_completo = montar_name_shopify(numero)
        variaveis = {
            "query": f'name:"{escapar_busca(name_completo)}"',
            "primeiros": TAMANHO_PAGINA,
        }

        async def chamar(timeout: float) -> dict[str, Any]:
            return await self._executar(variaveis, timeout)

        try:
            corpo = await com_reintento(chamar, self._politica, nome="Shopify orders")
        except ShopifyAcessoNegado:
            raise
        except (Transitorio, Permanente) as exc:
            raise ShopifyErro(redigir_excecao(exc)) from None

        return self._selecionar(corpo, numero)

    async def _executar(
        self, variaveis: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        try:
            token = await self._auth.obter()
        except ShopifyAutenticacaoErro as exc:
            # Credencial invalida ou app nao instalado: e configuracao, nao vale
            # repetir a chamada.
            raise ShopifyAcessoNegado(str(exc)) from None

        cabecalhos = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }
        corpo_req = {"query": CONSULTA, "variables": variaveis}

        try:
            if self._http is not None:
                resposta = await self._http.post(
                    self.url, json=corpo_req, headers=cabecalhos, timeout=timeout
                )
            else:
                async with httpx.AsyncClient(timeout=timeout) as http:
                    resposta = await http.post(
                        self.url, json=corpo_req, headers=cabecalhos
                    )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            raise Transitorio(redigir_excecao(exc)) from None
        except httpx.HTTPError as exc:
            raise Permanente(redigir_excecao(exc)) from None

        if resposta.status_code == 429 or resposta.status_code >= 500:
            raise Transitorio(f"Shopify devolveu HTTP {resposta.status_code}")
        if resposta.status_code in (401, 403):
            raise ShopifyAcessoNegado(
                f"Shopify recusou as credenciais (HTTP {resposta.status_code}). "
                "Verifique o token e o escopo read_orders."
            )
        if resposta.status_code >= 400:
            raise Permanente(f"Shopify devolveu HTTP {resposta.status_code}")

        try:
            corpo: Any = resposta.json()
        except ValueError:
            raise Permanente("resposta da Shopify nao e JSON valido") from None

        if not isinstance(corpo, dict):
            raise Permanente("resposta da Shopify nao e um objeto")

        self._checar_erros_graphql(corpo)
        return corpo

    @staticmethod
    def _checar_erros_graphql(corpo: dict[str, Any]) -> None:
        """A Shopify devolve HTTP 200 com `errors` no corpo.

        Falhamos FECHADO: se ha `errors`, descartamos o `data` mesmo que venha
        parcial. Esta consulta decide autorizacao -- aceitar dado parcial poderia
        validar um pedido com informacao incompleta.
        """
        erros = corpo.get("errors")
        if not erros:
            return

        codigos = {
            (e.get("extensions") or {}).get("code")
            for e in erros
            if isinstance(e, dict)
        }
        mensagens = "; ".join(
            str(e.get("message", "")) for e in erros if isinstance(e, dict)
        )

        if "ACCESS_DENIED" in codigos:
            raise ShopifyAcessoNegado(
                f"Shopify negou acesso (escopo insuficiente?): {mensagens}"
            )
        if codigos & _TRANSITORIOS:
            raise Transitorio(f"Shopify sinalizou falha transitoria: {mensagens}")
        raise Permanente(f"Shopify devolveu erros GraphQL: {mensagens}")

    def _selecionar(
        self, corpo: dict[str, Any], numero: NumeroPedidoFR
    ) -> PedidoShopify | None:
        dados = corpo.get("data") or {}
        orders = dados.get("orders") or {}
        arestas = orders.get("edges") or []

        for aresta in arestas:
            no = aresta.get("node") or {}
            name = no.get("name") or ""
            try:
                # Comparacao entre formas NORMALIZADAS: o `name` vem `#59552` e o
                # numero e `59552`. Compara-los literalmente falharia sempre.
                if NumeroPedidoFR(name) != numero:
                    continue
            except ValueError:
                # Pedido com nome fora do padrao configurado nao e o nosso.
                continue
            return self._montar(no, numero)

        # Nenhuma correspondencia: pode ser que a busca tenha trazido apenas
        # vizinhos. Registramos para nao ficar cego, sem fazer segunda chamada.
        page_info = orders.get("pageInfo") or {}
        if len(arestas) >= TAMANHO_PAGINA and page_info.get("hasNextPage"):
            logger.warning(
                "pagina cheia sem correspondencia exata para o pedido %s: %s",
                numero,
                Anomalia.PAGINA_CHEIA_SEM_CORRESPONDENCIA.value,
            )
        return None

    @staticmethod
    def _montar(no: dict[str, Any], numero: NumeroPedidoFR) -> PedidoShopify:
        anomalias: list[Anomalia] = []
        fulfillments = no.get("fulfillments") or []

        com_tracking = [
            f for f in fulfillments if (f.get("trackingInfo") or [])
        ]
        if len(com_tracking) > 1:
            # A regra de negocio informada e 1 pedido = 1 rastreio. Nao quebramos
            # a resposta, mas a violacao da premissa nao pode ficar escondida.
            anomalias.append(Anomalia.MULTIPLOS_FULFILLMENTS)

        codigo_rastreio: str | None = None
        if com_tracking:
            escolhido = max(com_tracking, key=lambda f: f.get("createdAt") or "")
            infos = escolhido.get("trackingInfo") or []
            if len(infos) > 1:
                # A ordem de `trackingInfo` e arbitraria porem consistente dentro
                # de uma resposta: nao ha garantia de que o primeiro seja o
                # "principal". Por isso a anomalia existe.
                anomalias.append(Anomalia.MULTIPLOS_TRACKINGS)
            numero_rastreio = (infos[0] or {}).get("number") if infos else None
            codigo_rastreio = numero_rastreio or None

        criado_em: datetime | None = None
        bruto = no.get("createdAt")
        if isinstance(bruto, str):
            try:
                criado_em = datetime.fromisoformat(bruto.replace("Z", "+00:00"))
            except ValueError:
                criado_em = None

        # A Shopify pode devolver `tags` como lista ou como string separada por
        # virgulas, conforme a versao e o campo consultado.
        brutas = no.get("tags") or []
        if isinstance(brutas, str):
            brutas = brutas.split(",")
        tags = [t.strip().casefold() for t in brutas if isinstance(t, str) and t.strip()]

        return PedidoShopify(
            id=str(no.get("id") or ""),
            name=str(no.get("name") or numero),
            email_normalizado=normalizar_email(no.get("email")),
            criado_em=criado_em,
            tem_fulfillment=bool(fulfillments),
            codigo_rastreio=codigo_rastreio,
            tags=tags,
            anomalias=anomalias,
        )
