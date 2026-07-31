"""Cliente da Frete Rapido -- Estrategia A: busca por numero do pedido.

`GET /api/external/embarcador/v1/quotes/{numero_pedido}/occurrences?token=...`

Validado com chamadas reais aos pedidos 59483 e 59552 em 30/07/2026.

A Estrategia B (busca por periodo) NAO existe aqui de proposito: a documentacao
impoe intervalo maximo de 4 horas e devolve apenas as ocorrencias ocorridas
dentro da janela, nao o historico. Nao serve como fallback.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.schemas import OcorrenciaFR
from app.services.logs import redigir, redigir_excecao
from app.services.normalizacao import NumeroPedidoFR
from app.services.ordenacao import indexar
from app.services.reintento import Permanente, Politica, Transitorio, com_reintento

logger = logging.getLogger(__name__)

_CAMINHO = "/api/external/embarcador/v1/quotes/{numero}/occurrences"

# Um frete real tem dezenas de ocorrencias, nao milhares.
MAX_OCORRENCIAS = 200


class FreteRapidoErro(Exception):
    """Falha ao consultar a Frete Rapido."""


class ClienteFreteRapido:
    def __init__(
        self,
        *,
        cliente_http: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        politica: Politica | None = None,
    ) -> None:
        s = get_settings()
        self._base_url = (base_url or s.frete_rapido_base_url).rstrip("/")
        self._http = cliente_http
        self._politica = politica or Politica()

    async def buscar_ocorrencias(
        self, numero: NumeroPedidoFR, token: str
    ) -> list[OcorrenciaFR]:
        """Ocorrencias de um pedido sob UM CNPJ especifico.

        O token e explicito porque a operacao usa tres CNPJs e cada token so
        enxerga os fretes do seu. Quem decide qual usar e o `BuscadorMultiCNPJ`.

        Aceita EXCLUSIVAMENTE `NumeroPedidoFR`. A anotacao ja barra `str` crua
        com mypy estrito, mas mypy nao roda em producao -- por isso a guarda
        abaixo tambem existe em runtime.
        """
        self._garantir_normalizado(numero)

        url = f"{self._base_url}{_CAMINHO.format(numero=numero)}"

        async def chamar(timeout: float) -> list[OcorrenciaFR]:
            return await self._executar(url, numero, token, timeout)

        try:
            return await com_reintento(
                chamar, self._politica, nome="Frete Rapido ocorrencias"
            )
        except (Transitorio, Permanente) as exc:
            # A mensagem original carrega a URL COM o token.
            raise FreteRapidoErro(redigir_excecao(exc)) from None

    @staticmethod
    def _garantir_normalizado(numero: NumeroPedidoFR) -> None:
        """Ultima barreira antes de montar a URL.

        Um "#" que escape aqui faria TODA consulta voltar vazia, sem erro nenhum.
        Falhar alto e infinitamente melhor do que devolver silencio.
        """
        if not isinstance(numero, NumeroPedidoFR):
            raise FreteRapidoErro(
                "numero de pedido nao normalizado chegou ao cliente da Frete Rapido: "
                f"{type(numero).__name__}"
            )
        if not numero.isdigit():
            raise FreteRapidoErro(
                f"numero de pedido nao normalizado chegou ao cliente: {numero!r}"
            )

    async def _executar(
        self, url: str, numero: str, token: str, timeout: float
    ) -> list[OcorrenciaFR]:
        params = {"token": token}
        try:
            if self._http is not None:
                resposta = await self._http.get(url, params=params, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as http:
                    resposta = await http.get(url, params=params)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            raise Transitorio(redigir_excecao(exc)) from None
        except httpx.HTTPError as exc:
            raise Permanente(redigir_excecao(exc)) from None

        if resposta.status_code == 429 or resposta.status_code >= 500:
            raise Transitorio(
                f"Frete Rapido devolveu HTTP {resposta.status_code} para o pedido {numero}"
            )
        if resposta.status_code >= 400:
            raise Permanente(
                f"Frete Rapido devolveu HTTP {resposta.status_code} para o pedido {numero}"
            )

        return self._parsear(resposta, numero)

    @staticmethod
    def _parsear(resposta: httpx.Response, numero: str) -> list[OcorrenciaFR]:
        try:
            dados: Any = resposta.json()
        except ValueError as exc:
            raise Permanente(
                f"resposta da Frete Rapido nao e JSON valido: {redigir(str(exc))}"
            ) from None

        # A resposta e um ARRAY puro, nao um objeto envelopado.
        if not isinstance(dados, list):
            raise Permanente(
                f"resposta da Frete Rapido nao e uma lista: {type(dados).__name__}"
            )

        # Teto na quantidade: um frete real tem dezenas de ocorrencias. Milhares
        # indicam erro do fornecedor ou payload hostil, e carregar tudo incharia
        # cache, logs e o DOM da pagina.
        if len(dados) > MAX_OCORRENCIAS:
            logger.warning(
                "pedido %s veio com %d ocorrencias; truncado em %d",
                numero,
                len(dados),
                MAX_OCORRENCIAS,
            )
            dados = dados[:MAX_OCORRENCIAS]

        ocorrencias: list[OcorrenciaFR] = []
        for bruta in dados:
            try:
                # `model_validate` aplica a LISTA DE PERMISSAO: campos como
                # `cnpj_cpf_entregador`, `nome_entregador` e `comprovantes` sao
                # descartados aqui e nunca chegam ao cache ou aos logs.
                ocorrencias.append(OcorrenciaFR.model_validate(bruta))
            except ValueError as exc:
                # Uma ocorrencia malformada nao pode derrubar o historico inteiro.
                logger.warning(
                    "ocorrencia ignorada no pedido %s: %s", numero, redigir(str(exc))
                )

        # Se a API devolveu itens e NENHUM foi aproveitado, o formato mudou.
        # Tratar como lista vazia diria ao cliente "pedido em separacao" -- uma
        # mentira tranquilizadora que esconderia a quebra da integracao ate
        # alguem reclamar. Melhor falhar alto.
        if dados and not ocorrencias:
            raise Permanente(
                f"pedido {numero}: {len(dados)} ocorrencia(s) recebidas e nenhuma "
                "reconhecida -- o formato da Frete Rapido pode ter mudado"
            )

        return indexar(ocorrencias)
