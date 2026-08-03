"""Decide se uma ocorrencia vira aviso ao cliente, e manda o n8n entregar.

A ORDEM dos passos e normativa, como em `consulta.py`, e por motivos parecidos:

1. **Filtrar antes de consultar a Shopify.** A Frete Rapido manda webhook para
   TODA ocorrencia -- "Contratado", "Em transito", "Entregue". A maioria esmagadora
   nao vira mensagem, e gastar uma chamada a Shopify em cada uma seria desperdicio
   proporcional ao volume da operacao.
2. **Deduplicar antes de agir.** A FR reenvia o mesmo evento ate 12 vezes em ~24h
   enquanto nao receber HTTP 200. Sem a reserva no banco, cada reentrega viraria
   uma mensagem nova -- o pior modo de falha deste projeto.
3. **Resolver o pedido na Shopify antes de enviar qualquer coisa.** O webhook NAO
   e assinado: o `numero_pedido` do payload e dado de origem nao confiavel. Isso
   limita o estrago de um segredo vazado a "mensagem sobre um pedido real para o
   dono real dele".

O desfecho `pendente` e o unico que responde 503, e isso e proposital: a escada
de reentrega da propria Frete Rapido (1, 2, 3, 5, 10, 30 min, depois 1, 2, 3, 4,
5, 8 h) faz o papel de fila de reprocessamento, sem precisarmos manter uma.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import Settings, get_settings
from app.schemas import Grupo, StatusEvento, WebhookOcorrenciaFR
from app.services.datas import atribuir_fuso
from app.services.eventos import ChaveEvento, EventosMemoria, RepositorioEventos
from app.services.logs import redigir_excecao
from app.services.n8n import ClienteN8n, N8nErro
from app.services.normalizacao import NumeroPedidoErro, NumeroPedidoFR, truncar
from app.services.ocorrencias import classificar
from app.services.shopify import ClienteShopify, ShopifyErro
from app.services.transportadora import nome_amigavel

logger = logging.getLogger(__name__)

MAX_ERRO = 256


@dataclass(frozen=True)
class Desfecho:
    """O que aconteceu com o evento, e o que responder a Frete Rapido."""

    status: StatusEvento
    grupo: Grupo
    detalhe: str | None = None

    @property
    def status_http(self) -> int:
        """503 apenas em `pendente`: e o pedido de reenvio a Frete Rapido.

        Todos os demais desfechos sao TERMINAIS e respondem 200. Devolver erro
        num evento que nunca podera ser entregue -- pedido sem telefone, por
        exemplo -- so gastaria as 12 tentativas dela sem mudar nada.
        """
        return 503 if self.status is StatusEvento.PENDENTE else 200


class ServicoNotificacao:
    def __init__(
        self,
        shopify: ClienteShopify,
        eventos: RepositorioEventos | None = None,
        n8n: ClienteN8n | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._shopify = shopify
        self._eventos: RepositorioEventos = eventos or EventosMemoria()
        self._n8n = n8n or ClienteN8n()
        self._s = settings or get_settings()

    # ------------------------------------------------------------------
    # Decisao de gatilho
    # ------------------------------------------------------------------

    def deve_notificar(self, codigo: int, grupo: Grupo) -> bool:
        """Regra de gatilho, nesta ordem: ignorados, extra, grupo.

        Os ignorados vem primeiro para que desligar um codigo especifico seja
        sempre possivel, sem precisar desmontar o grupo inteiro.
        """
        if codigo in self._s.codigos_ignorados:
            return False
        if codigo in self._s.codigos_extra:
            return True
        return grupo in self._s.grupos_notificaveis

    # ------------------------------------------------------------------
    # Fluxo
    # ------------------------------------------------------------------

    async def processar(self, evento: WebhookOcorrenciaFR) -> Desfecho:
        grupo = classificar(evento.codigo)

        # O numero vem de fora e pode chegar em qualquer formato. Sem forma
        # canonica nao ha como casar com a Shopify nem deduplicar de forma
        # estavel -- duas grafias do mesmo pedido furariam a chave UNIQUE.
        try:
            numero = NumeroPedidoFR(evento.numero_pedido)
        except NumeroPedidoErro:
            logger.warning(
                "webhook com numero de pedido em formato inesperado (codigo %s)",
                evento.codigo,
            )
            return Desfecho(StatusEvento.DESCARTADO, grupo, "numero nao normalizavel")

        chave = ChaveEvento(
            numero_pedido=str(numero),
            codigo=evento.codigo,
            data_ocorrencia=atribuir_fuso(evento.data_ocorrencia),
        )

        # 1. Gatilho. O caso comum -- e o mais barato.
        if not self.deve_notificar(evento.codigo, grupo):
            await self._eventos.registrar(chave, grupo, StatusEvento.DESCARTADO)
            return Desfecho(StatusEvento.DESCARTADO, grupo)

        # 2. Reserva. Um status terminal ja gravado significa que este evento ja
        # foi resolvido: e uma reentrega da FR, e responder 200 a encerra.
        existente = await self._eventos.reservar(chave, grupo)
        if existente is not None and existente is not StatusEvento.PENDENTE:
            logger.info(
                "evento repetido do pedido %s (codigo %s): ja estava %s",
                numero,
                evento.codigo,
                existente.value,
            )
            return Desfecho(existente, grupo, "reentrega")

        # 3. Trava anti-spam. Vale por pedido, nao por evento: uma transportadora
        # que posta cinco codigos em sequencia nao pode virar cinco mensagens.
        # Tambem e o limitador de estrago caso o segredo da rota vaze.
        desde = datetime.now(UTC) - timedelta(hours=self._s.notificacao_janela_horas)
        recentes = await self._eventos.contar_avisos(str(numero), desde)
        if recentes >= self._s.notificacao_max_por_pedido:
            motivo = (
                f"limite anti-spam: {recentes} aviso(s) em "
                f"{self._s.notificacao_janela_horas}h"
            )
            logger.warning("aviso contido para o pedido %s -- %s", numero, motivo)
            await self._eventos.concluir(chave, StatusEvento.DESCARTADO, motivo)
            return Desfecho(StatusEvento.DESCARTADO, grupo, motivo)

        # 4. Shopify: e aqui que o payload nao confiavel encontra a realidade.
        try:
            pedido = await self._shopify.buscar_pedido(numero)
        except ShopifyErro as exc:
            # Shopify fora do ar nao pode PERDER o aviso: fica pendente e a FR
            # reenvia. E o caso em que o 503 vale a pena.
            falha = truncar(redigir_excecao(exc), MAX_ERRO)
            logger.error("falha na Shopify ao processar webhook: %s", falha)
            await self._eventos.concluir(chave, StatusEvento.PENDENTE, falha)
            return Desfecho(StatusEvento.PENDENTE, grupo, falha)

        if pedido is None:
            # Pedido que nao existe na loja: webhook forjado, numero de outra
            # operacao, ou pedido antigo demais para a API da Shopify.
            logger.warning("webhook para pedido inexistente na Shopify: %s", numero)
            await self._eventos.concluir(
                chave, StatusEvento.DESCARTADO, "pedido inexistente na Shopify"
            )
            return Desfecho(StatusEvento.DESCARTADO, grupo, "pedido inexistente")

        if not pedido.telefone:
            # Terminal, e nao falha: sem numero utilizavel nao ha o que reenviar.
            # A taxa disto e o que decide se vale buscar o telefone na propria
            # Frete Rapido (`quote/{id_frete}`) como segunda fonte.
            await self._eventos.concluir(chave, StatusEvento.SEM_CONTATO)
            return Desfecho(StatusEvento.SEM_CONTATO, grupo)

        # 5. Interruptor geral. Ate aqui tudo rodou de verdade -- inclusive a
        # consulta a Shopify -- e e isso que da a medicao real da Fase 1.
        if not self._s.notificacao_ativa:
            await self._eventos.concluir(chave, StatusEvento.OBSERVADO)
            logger.info(
                "aviso OBSERVADO (envio desligado): pedido %s, codigo %s, grupo %s",
                numero,
                evento.codigo,
                grupo.value,
            )
            return Desfecho(StatusEvento.OBSERVADO, grupo)

        # 6. Entrega.
        payload = montar_payload(evento, grupo, pedido.telefone, pedido.nome_cliente)
        try:
            await self._n8n.enviar(payload)
        except N8nErro as exc:
            falha = truncar(redigir_excecao(exc), MAX_ERRO)
            logger.error("falha ao entregar aviso ao n8n: %s", falha)
            await self._eventos.concluir(chave, StatusEvento.PENDENTE, falha)
            return Desfecho(StatusEvento.PENDENTE, grupo, falha)

        await self._eventos.concluir(chave, StatusEvento.ENVIADO)
        logger.info(
            "aviso enviado: pedido %s, codigo %s, grupo %s",
            numero,
            evento.codigo,
            grupo.value,
        )
        return Desfecho(StatusEvento.ENVIADO, grupo)


def montar_payload(
    evento: WebhookOcorrenciaFR,
    grupo: Grupo,
    telefone: str,
    nome_cliente: str | None,
) -> dict[str, Any]:
    """Contrato com o n8n.

    Deliberadamente MINIMO no que e dado pessoal -- telefone e primeiro nome, e
    nada mais. O n8n retem os dados de execucao no banco dele, entao tudo que
    sai daqui fica fora do alcance de `scripts/expurgar.py`.

    `rotulo` e o campo `nome` da propria Frete Rapido, NAO traduzido: mesma
    regra de `ocorrencias.py`. O catalogo muda sozinho e sempre em portugues
    legivel; uma traducao nossa envelheceria.
    """
    data = atribuir_fuso(evento.data_ocorrencia)
    return {
        "evento": "acao_necessaria",
        "grupo": grupo.value,
        "pedido": evento.numero_pedido,
        "codigo": evento.codigo,
        "rotulo": evento.nome,
        "mensagem": evento.mensagem,
        "transportadora": nome_amigavel(evento.nome_transportadora),
        "telefone": telefone,
        "primeiro_nome": nome_cliente,
        "prazo_devolucao": evento.prazo_devolucao,
        "prazo_entrega_consumidor": evento.prazo_entrega_consumidor,
        "data_ocorrencia": data.isoformat() if data else None,
    }
