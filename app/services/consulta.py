"""Orquestracao da consulta de rastreio.

A ORDEM dos passos e normativa e duas decisoes nela sao de seguranca:

- O cache so e consultado DEPOIS da validacao do email (passo 6, nunca antes do
  5). Consultar antes devolveria dados de um pedido alheio a quem soubesse
  apenas o numero.
- A Frete Rapido e chamada mesmo quando nao ha fulfillment na Shopify. A busca
  precisa apenas do numero do pedido, e os proprios pedidos de teste provam a
  necessidade: retornam "Contratado" e "Aguardando coleta", estados anteriores
  ao despacho, quando ainda nao existe codigo de rastreio.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.schemas import (
    Anomalia,
    EventoResposta,
    Grupo,
    OcorrenciaFR,
    RespostaConsulta,
    RespostaErro,
    RespostaSemRastreio,
    RespostaSucesso,
    RespostaVazioFR,
    Resultado,
)
from app.services import consolidacao
from app.services.cache import Cache, CacheDesligado
from app.services.datas import atribuir_fuso, entrega_atrasada
from app.services.multi_cnpj import BuscadorMultiCNPJ
from app.services.normalizacao import NumeroPedidoErro, NumeroPedidoFR
from app.services.ocorrencias import classificar
from app.services.ordenacao import ordenar_desc
from app.services.shopify import ClienteShopify, ShopifyErro
from app.services.transportadora import nome_amigavel

logger = logging.getLogger(__name__)

# Mensagem UNICA para pedido inexistente e para email divergente. Diferenciar
# permitiria descobrir quais numeros de pedido existem.
MSG_NAO_ENCONTRADO = (
    "Nao encontramos um pedido com esses dados. Confira o email e o numero do "
    "pedido. Compras com mais de 60 dias nao aparecem nesta consulta."
)
MSG_SEM_RASTREIO = (
    "Pedido confirmado e em separacao. O rastreio aparece aqui assim que ele "
    "for despachado."
)
MSG_VAZIO_FR = (
    "Seu pedido ja foi despachado, mas nao conseguimos obter o status detalhado "
    "agora. Tente novamente em alguns minutos."
)
# Nao exibimos codigo de rastreio: o que o ERP grava no fulfillment e o
# `id_frete` da Frete Rapido, que nao funciona no site da transportadora.
MSG_ERRO_EXTERNO = (
    "Nao foi possivel consultar o rastreio no momento. Tente novamente em alguns "
    "minutos."
)


@dataclass
class Consulta:
    """Resposta ao cliente mais os metadados que alimentam o log de auditoria."""

    resposta: RespostaConsulta
    resultado: Resultado
    anomalias: list[Anomalia] = field(default_factory=list)
    veio_do_cache: bool = False
    # Qual dos CNPJs atendeu, para auditoria.
    cnpj: str | None = None

    @property
    def status_http(self) -> int:
        return {
            Resultado.SUCESSO: 200,
            Resultado.SEM_RASTREIO: 200,
            Resultado.VAZIO_FR: 200,
            Resultado.NAO_ENCONTRADO: 404,
            Resultado.ERRO_EXTERNO: 503,
        }[self.resultado]


def _nao_encontrado() -> Consulta:
    return Consulta(
        resposta=RespostaErro(
            resultado=Resultado.NAO_ENCONTRADO, mensagem=MSG_NAO_ENCONTRADO
        ),
        resultado=Resultado.NAO_ENCONTRADO,
    )


def _erro_externo() -> Consulta:
    return Consulta(
        resposta=RespostaErro(
            resultado=Resultado.ERRO_EXTERNO, mensagem=MSG_ERRO_EXTERNO
        ),
        resultado=Resultado.ERRO_EXTERNO,
    )


def _para_evento(o: OcorrenciaFR) -> EventoResposta:
    return EventoResposta(
        codigo=o.codigo,
        rotulo=o.nome,  # o rotulo e o `nome` da propria API; nao traduzimos
        grupo=classificar(o.codigo),
        data=atribuir_fuso(o.data_ocorrencia),
        descricao=o.descricao,
    )


class ServicoConsulta:
    def __init__(
        self,
        shopify: ClienteShopify,
        frete_rapido: BuscadorMultiCNPJ,
        cache: Cache | None = None,
    ) -> None:
        self._shopify = shopify
        self._frete_rapido = frete_rapido
        self._cache: Cache = cache or CacheDesligado()

    async def consultar(self, email: str, numero_digitado: str) -> Consulta:
        # 3. Normalizar. Formato inesperado nao existe como pedido.
        try:
            numero = NumeroPedidoFR(numero_digitado)
        except NumeroPedidoErro:
            return _nao_encontrado()

        # 4. Buscar na Shopify.
        try:
            pedido = await self._shopify.buscar_pedido(numero)
        except ShopifyErro as exc:
            logger.error("falha ao consultar a Shopify: %s", exc)
            return _erro_externo()

        if pedido is None:
            return _nao_encontrado()

        # 5. Validar o email. Falha aqui responde EXATAMENTE como pedido
        #    inexistente.
        if not pedido.email_confere(email):
            return _nao_encontrado()

        # A partir daqui o titular esta validado.
        numero_confirmado = NumeroPedidoFR(pedido.name)
        anomalias = list(pedido.anomalias)

        # 6. Cache -- somente agora.
        veio_do_cache = False
        cnpj: str | None = None
        ocorrencias = await self._cache.obter(numero_confirmado)

        if ocorrencias is None:
            # 7. Frete Rapido, independentemente de haver fulfillment.
            #    O CNPJ e escolhido pela tag do pedido (ver multi_cnpj).
            busca = await self._frete_rapido.buscar(numero_confirmado, pedido.tags)
            anomalias.extend(busca.anomalias)
            ocorrencias = busca.ocorrencias
            cnpj = busca.cnpj

            # FALHA FECHADA: sem dados E com falha em algum CNPJ, nao da para
            # afirmar que o pedido nao foi despachado -- o token que falhou pode
            # ser justamente o que tinha as ocorrencias.
            if not ocorrencias and busca.houve_falha:
                logger.error(
                    "pedido %s sem dados e com falha em ao menos um CNPJ",
                    numero_confirmado,
                )
                return _erro_externo()

            await self._cache.guardar(numero_confirmado, ocorrencias)
        else:
            veio_do_cache = True

        # O codigo de rastreio NUNCA vem do cache: a Shopify e consultada em toda
        # requisicao de qualquer forma, entao usamos sempre o dado fresco.
        codigo_rastreio = pedido.codigo_rastreio

        if not ocorrencias:
            if pedido.tem_fulfillment and codigo_rastreio:
                # Anomalia: ha despacho na Shopify mas a Frete Rapido nao conhece
                # o pedido. Alertado por TAXA, nunca por evento isolado.
                logger.warning(
                    "vazio_fr no pedido %s (ha fulfillment com rastreio)",
                    numero_confirmado,
                )
                return Consulta(
                    resposta=RespostaVazioFR(
                        pedido=pedido.name,
                        mensagem=MSG_VAZIO_FR,
                    ),
                    resultado=Resultado.VAZIO_FR,
                    anomalias=anomalias,
                    veio_do_cache=veio_do_cache,
                    cnpj=cnpj,
                )
            return Consulta(
                resposta=RespostaSemRastreio(
                    pedido=pedido.name, mensagem=MSG_SEM_RASTREIO
                ),
                resultado=Resultado.SEM_RASTREIO,
                anomalias=anomalias,
                veio_do_cache=veio_do_cache,
                cnpj=cnpj,
            )

        # 8. Ordenar (desempate estavel) e consolidar campos.
        ordenadas = ordenar_desc(ocorrencias)
        historico = [_para_evento(o) for o in ordenadas]
        status_atual = historico[0]

        previsao = consolidacao.previsao_entrega(ordenadas)
        entregue = status_atual.grupo is Grupo.ENTREGUE

        return Consulta(
            resposta=RespostaSucesso(
                pedido=pedido.name,
                status_atual=status_atual,
                historico=historico,
                # Razao social -> nome comercial: o cliente conhece "Correios",
                # nao "EMPRESA BRASILEIRA DE CORREIOS E TELEGRAFOS".
                transportadora=nome_amigavel(consolidacao.transportadora(ordenadas)),
                previsao_entrega=previsao,
                entrega_atrasada=entrega_atrasada(previsao, entregue),
            ),
            resultado=Resultado.SUCESSO,
            anomalias=anomalias,
            veio_do_cache=veio_do_cache,
            cnpj=cnpj,
        )
