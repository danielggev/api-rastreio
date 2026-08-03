"""Decide se uma ocorrencia vira aviso ao cliente, e manda o n8n entregar.

A ORDEM dos passos e normativa, como em `consulta.py`, e por motivos parecidos:

1. **Filtrar antes de consultar a Shopify.** A Frete Rapido manda webhook para
   TODA ocorrencia -- "Contratado", "Em transito", "Entregue". A maioria esmagadora
   nao vira mensagem, e gastar uma chamada a Shopify em cada uma seria desperdicio
   proporcional ao volume da operacao.
2. **Deduplicar antes de agir.** A FR reenvia o mesmo evento ate 12 vezes em ~24h
   enquanto nao receber HTTP 200. Sem a reserva no banco, cada reentrega viraria
   uma mensagem nova -- o pior modo de falha deste projeto.
3. **Confirmar o evento na propria Frete Rapido antes de tocar em dado do
   cliente.** O webhook NAO e assinado: o segredo da URL prova que quem chamou o
   conhece, nao que o evento aconteceu. So a Frete Rapido tem autoridade sobre
   isso, e perguntar a ela nao depende de IP fixo nem de nada que o fornecedor
   precise nos conceder.
4. **Resolver o pedido na Shopify.** O `numero_pedido` do payload tambem e dado
   de origem nao confiavel, e a Shopify e quem tem o contato do cliente.

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
from app.services.multi_cnpj import BuscadorMultiCNPJ
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
        frete_rapido: BuscadorMultiCNPJ | None = None,
    ) -> None:
        self._shopify = shopify
        self._eventos: RepositorioEventos = eventos or EventosMemoria()
        self._n8n = n8n or ClienteN8n()
        self._s = settings or get_settings()
        # Usado para CONFIRMAR o evento na origem. Sem ele a verificacao e
        # pulada -- o que so acontece em teste, ou com o interruptor desligado.
        self._frete_rapido = frete_rapido

    async def _confirmado_na_fonte(
        self, numero: NumeroPedidoFR, codigo: int, cnpj: str | None
    ) -> tuple[bool, str | None]:
        """A Frete Rapido confirma que este pedido tem esta ocorrencia?

        Devolve `(confirmado, motivo_da_falha)`. Falha de comunicacao e
        confirmacao negativa sao coisas diferentes para quem chama, mas ambas
        impedem o envio.

        Casamos apenas o CODIGO, nao a data. Nao e concessao: a pergunta que
        importa e "esta encomenda esta mesmo aguardando retirada AGORA?". Se a
        Frete Rapido diz que sim, a mensagem e verdadeira -- independente de o
        webhook ter sido genuino ou reproduzido por alguem. O dano que estamos
        evitando e a mensagem FALSA, e casar so o codigo ja o elimina.
        Exigir data exata acrescentaria pouco e criaria uma dependencia fragil
        entre dois endpoints que podem formatar o instante de forma diferente --
        se divergissem, NENHUM aviso sairia, e em silencio.
        """
        if self._frete_rapido is None:
            return True, None

        # A tag da consulta e o proprio CNPJ do cadastro: ja sabemos qual token
        # usar, sem depender das tags da Shopify. Vazio cai no caminho seguro do
        # buscador, que consulta todos em paralelo.
        tags = [cnpj] if cnpj else []
        try:
            resultado = await self._frete_rapido.buscar(numero, tags)
        except Exception as exc:
            return False, truncar(redigir_excecao(exc), MAX_ERRO)

        if resultado.houve_falha and not resultado.ocorrencias:
            return False, "falha ao consultar a Frete Rapido para confirmar"

        if any(o.codigo == codigo for o in resultado.ocorrencias):
            return True, None

        return False, f"ocorrencia {codigo} nao confirmada na Frete Rapido"

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

    async def processar(
        self, evento: WebhookOcorrenciaFR, cnpj: str | None = None
    ) -> Desfecho:
        """`cnpj` vem do segredo da URL, nao do payload.

        A Frete Rapido nao diz qual embarcador originou o evento -- o unico CNPJ
        no corpo e o da transportadora. Como ha um cadastro de webhook por CNPJ,
        o segredo do caminho carrega essa identidade.
        """
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
            await self._eventos.registrar(
                chave, grupo, StatusEvento.DESCARTADO, cnpj=cnpj
            )
            return Desfecho(StatusEvento.DESCARTADO, grupo)

        # 2. Reserva. Um status terminal ja gravado significa que este evento ja
        # foi resolvido: e uma reentrega da FR, e responder 200 a encerra.
        existente = await self._eventos.reservar(chave, grupo, cnpj=cnpj)
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

        # 4. Confirmar na FONTE, antes de tocar em dado do cliente.
        #
        # A ordem importa em dois sentidos. Primeiro, seguranca: o segredo da URL
        # prova que quem chamou o conhece, nao que o evento aconteceu -- so a
        # Frete Rapido tem autoridade sobre isso. Segundo, privacidade: nao
        # buscamos o telefone de ninguem com base num evento que ainda nao
        # sabemos se e real.
        #
        # Nao confirmado vira PENDENTE, e nao descarte: o webhook pode chegar
        # antes de a leitura refletir o evento, e a escada de reentrega da propria
        # Frete Rapido (1, 2, 3, 5, 10 min...) resolve isso sozinha. Um evento
        # forjado, esse sim, nunca confirma -- esgota as tentativas e fica
        # visivel no relatorio 13 em vez de virar mensagem.
        if self._s.notificacao_verificar_na_fonte:
            confirmado, porque = await self._confirmado_na_fonte(
                numero, evento.codigo, cnpj
            )
            if not confirmado:
                logger.warning(
                    "evento do pedido %s (codigo %s) nao confirmado na fonte: %s",
                    numero,
                    evento.codigo,
                    porque,
                )
                await self._eventos.concluir(chave, StatusEvento.PENDENTE, porque)
                return Desfecho(StatusEvento.PENDENTE, grupo, porque)

        # 5. Shopify: e aqui que o payload nao confiavel encontra a realidade.
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

        # 6. Interruptor geral. Ate aqui tudo rodou de verdade -- inclusive a
        # confirmacao na fonte e a consulta a Shopify -- e e isso que da a
        # medicao real da Fase 1.
        if not self._s.notificacao_ativa:
            await self._eventos.concluir(chave, StatusEvento.OBSERVADO)
            logger.info(
                "aviso OBSERVADO (envio desligado): pedido %s, codigo %s, grupo %s",
                numero,
                evento.codigo,
                grupo.value,
            )
            return Desfecho(StatusEvento.OBSERVADO, grupo)

        # 7. Entrega.
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
