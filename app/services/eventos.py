"""Persistencia dos eventos recebidos por webhook.

Existe separado de `notificacao.py` pelo mesmo motivo que `cache.py` existe
separado de `consulta.py`: a decisao de negocio precisa ser testavel sem
Postgres no ar. Ha duas implementacoes, e a de memoria nao e so um dublê de
teste -- e o que roda quando `DATABASE_URL` nao esta configurada, coerente com
a postura do projeto de servir o cliente mesmo sem infraestrutura completa.

A tabela nao guarda NENHUM dado pessoal. O contato e lido da Shopify no momento
do envio e descartado; aqui fica so o registro de que o aviso saiu.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import (
    ColumnElement,
    and_,
    delete,
    func,
    not_,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import EventoFrete
from app.db.session import sessao
from app.schemas import Grupo, StatusEvento

logger = logging.getLogger(__name__)

# Statuses que contam para a trava anti-spam: o que foi enviado e o que TERIA
# sido enviado. Incluir `observado` faz o modo de observacao simular de verdade
# o volume da Fase 2, em vez de subestima-lo.
_CONTAM_COMO_AVISO = (StatusEvento.ENVIADO, StatusEvento.OBSERVADO)

# Sentinela para "nao filtrar por CNPJ". `None` nao serve: e um CNPJ valido
# (eventos que chegaram pelo segredo avulso) e precisa de teto proprio.
_SEM_FILTRO: str = "\x00__todos__"


@dataclass(frozen=True)
class ChaveEvento:
    """Identidade de uma ocorrencia, para deduplicacao.

    `data_ocorrencia` faz parte da chave de proposito: sem ela, uma segunda
    tentativa de entrega legitima (mesmo pedido, mesmo codigo, dias depois)
    seria confundida com uma reentrega do webhook e o cliente nao seria avisado.
    """

    numero_pedido: str
    codigo: int
    data_ocorrencia: datetime | None


@dataclass(frozen=True)
class Reserva:
    """Resultado de tentar assumir um evento para PROCESSAR.

    Trata apenas de exclusividade e de CUSTO. A cota de MENSAGENS e outra
    decisao, tomada depois da confirmacao na fonte -- ver `reservar_aviso`.
    Junta-las fazia evento forjado ocupar vaga de aviso antes de qualquer
    verificacao, barrando o legitimo que chegasse junto.

    Exatamente UM dos campos manda:

    - `adquirida` -- somos donos do lease; siga.
    - `status` -- ja havia desfecho terminal gravado (reentrega da Frete Rapido).
    - `em_andamento` -- outro processo tem o lease vivo.
    - `em_espera` -- a linha esta em cooldown; repetir agora nao ajuda.
    - `custo_excedido` -- pedido com tentativas demais na janela.
    """

    adquirida: bool = False
    status: StatusEvento | None = None
    em_andamento: bool = False
    em_espera: bool = False
    custo_excedido: bool = False
    tentativas_recentes: int = 0


@dataclass(frozen=True)
class ReservaAviso:
    """Resultado de tentar ocupar uma vaga de MENSAGEM.

    Tomada apos a confirmacao na fonte, para que so evento verificado consuma
    cota.
    """

    concedida: bool = False
    cota_excedida: bool = False
    # Ja avisamos sobre ESTE codigo neste pedido, dentro da janela CURTA de
    # agregacao de volume. Separado de `cota_excedida` porque o motivo e outro:
    # aqui nao houve excesso, houve repeticao do mesmo fato.
    codigo_repetido: bool = False
    # Disjuntor sistemico (global ou do CNPJ). NAO e caso de descarte: o evento
    # provavelmente e legitimo e so chegou numa rajada. Quem chama responde 503
    # para a Frete Rapido reapresentar depois.
    limite_sistemico: str | None = None
    avisos_recentes: int = 0


class RepositorioEventos(Protocol):
    async def adquirir(
        self,
        chave: ChaveEvento,
        grupo: Grupo,
        *,
        cnpj: str | None = None,
        dono: str,
        lease_s: int,
        cooldown_s: int,
        desde: datetime,
        max_tentativas: int,
    ) -> Reserva: ...

    async def reservar_aviso(
        self,
        chave: ChaveEvento,
        *,
        desde: datetime,
        desde_volume: datetime,
        desde_hora: datetime,
        max_avisos: int,
        cnpj: str | None = None,
        max_global: int,
        max_cnpj: int,
    ) -> ReservaAviso: ...

    async def renovar(
        self, chave: ChaveEvento, *, dono: str, lease_s: int
    ) -> bool: ...

    async def registrar(
        self,
        chave: ChaveEvento,
        grupo: Grupo,
        status: StatusEvento,
        erro: str | None = None,
        cnpj: str | None = None,
    ) -> None: ...

    async def concluir(
        self,
        chave: ChaveEvento,
        status: StatusEvento,
        erro: str | None = None,
        *,
        dono: str | None = None,
    ) -> None: ...

    async def contar_avisos(self, numero_pedido: str, desde: datetime) -> int: ...


@dataclass
class _Linha:
    status: StatusEvento
    recebido_em: datetime
    processando_ate: datetime | None = None
    dono: str | None = None
    proxima_tentativa_em: datetime | None = None
    aviso_reservado_em: datetime | None = None
    cnpj: str | None = None


class EventosMemoria:
    """Implementacao em memoria. Usada nos testes e quando nao ha banco.

    Sem banco a deduplicacao so vale enquanto o processo viver -- uma reentrega
    da Frete Rapido depois de um reinicio passaria de novo. E uma degradacao
    aceita conscientemente: em producao `DATABASE_URL` e obrigatoria.
    """

    def __init__(self) -> None:
        self._linhas: dict[ChaveEvento, _Linha] = {}

    async def adquirir(
        self,
        chave: ChaveEvento,
        grupo: Grupo,
        *,
        cnpj: str | None = None,
        dono: str,
        lease_s: int,
        cooldown_s: int,
        desde: datetime,
        max_tentativas: int,
    ) -> Reserva:
        # Sem `await` no corpo: em asyncio isto ja e atomico entre corrotinas, o
        # que reproduz o lock consultivo da implementacao Postgres.
        agora = datetime.now(UTC)
        linha = self._linhas.get(chave)

        if linha is not None:
            if linha.status is not StatusEvento.PENDENTE:
                return Reserva(status=linha.status)
            if linha.processando_ate and linha.processando_ate > agora:
                return Reserva(em_andamento=True)
            if linha.proxima_tentativa_em and linha.proxima_tentativa_em > agora:
                return Reserva(em_espera=True)

        tentativas = self._contar_tentativas(chave.numero_pedido, desde)
        if linha is None and tentativas >= max_tentativas:
            return Reserva(custo_excedido=True, tentativas_recentes=tentativas)

        self._linhas[chave] = _Linha(
            status=StatusEvento.PENDENTE,
            recebido_em=linha.recebido_em if linha else agora,
            processando_ate=agora + timedelta(seconds=lease_s),
            dono=dono,
            proxima_tentativa_em=agora + timedelta(seconds=cooldown_s),
            aviso_reservado_em=linha.aviso_reservado_em if linha else None,
            cnpj=cnpj if cnpj is not None else (linha.cnpj if linha else None),
        )
        return Reserva(adquirida=True)

    async def reservar_aviso(
        self,
        chave: ChaveEvento,
        *,
        desde: datetime,
        desde_volume: datetime,
        desde_hora: datetime,
        max_avisos: int,
        cnpj: str | None = None,
        max_global: int,
        max_cnpj: int,
    ) -> ReservaAviso:
        linha = self._linhas.get(chave)
        if linha is not None and linha.aviso_reservado_em is not None:
            return ReservaAviso(concedida=True)

        repetido = self._contar_reservados(chave, desde_volume, codigo=chave.codigo)
        if repetido:
            return ReservaAviso(
                cota_excedida=True, codigo_repetido=True, avisos_recentes=repetido
            )

        avisos = self._contar_reservados(chave, desde)
        if avisos >= max_avisos:
            return ReservaAviso(cota_excedida=True, avisos_recentes=avisos)

        no_cnpj = self._contar_na_hora(desde_hora, cnpj=cnpj)
        if no_cnpj >= max_cnpj:
            return ReservaAviso(
                limite_sistemico=f"CNPJ {cnpj or '(sem id)'}: {no_cnpj} avisos/h",
                avisos_recentes=no_cnpj,
            )

        total = self._contar_na_hora(desde_hora)
        if total >= max_global:
            return ReservaAviso(
                limite_sistemico=f"loja inteira: {total} avisos/h",
                avisos_recentes=total,
            )

        if linha is not None:
            linha.aviso_reservado_em = datetime.now(UTC)
            linha.cnpj = cnpj
        return ReservaAviso(concedida=True)

    def _contar_na_hora(
        self, desde: datetime, cnpj: str | None = _SEM_FILTRO
    ) -> int:
        return sum(
            1
            for linha in self._linhas.values()
            if linha.aviso_reservado_em is not None
            and linha.aviso_reservado_em >= desde
            and (cnpj is _SEM_FILTRO or linha.cnpj == cnpj)
        )

    async def renovar(self, chave: ChaveEvento, *, dono: str, lease_s: int) -> bool:
        linha = self._linhas.get(chave)
        if linha is None or linha.dono != dono:
            return False
        linha.processando_ate = datetime.now(UTC) + timedelta(seconds=lease_s)
        return True

    def _contar_tentativas(self, numero_pedido: str, desde: datetime) -> int:
        return sum(
            1
            for chave, linha in self._linhas.items()
            if chave.numero_pedido == numero_pedido
            and linha.recebido_em >= desde
            and linha.status is not StatusEvento.DESCARTADO
        )

    def _contar_reservados(
        self, chave: ChaveEvento, desde: datetime, codigo: int | None = None
    ) -> int:
        return sum(
            1
            for outra, linha in self._linhas.items()
            if outra != chave
            and outra.numero_pedido == chave.numero_pedido
            and linha.aviso_reservado_em is not None
            and linha.aviso_reservado_em >= desde
            and (codigo is None or outra.codigo == codigo)
        )

    async def registrar(
        self,
        chave: ChaveEvento,
        grupo: Grupo,
        status: StatusEvento,
        erro: str | None = None,
        cnpj: str | None = None,
    ) -> None:
        self._linhas.setdefault(
            chave, _Linha(status=status, recebido_em=datetime.now(UTC))
        )

    async def concluir(
        self,
        chave: ChaveEvento,
        status: StatusEvento,
        erro: str | None = None,
        *,
        dono: str | None = None,
    ) -> None:
        linha = self._linhas.get(chave)
        if dono is not None and linha is not None and linha.dono not in (None, dono):
            return  # fencing: o lease ja e de outro
        self._linhas[chave] = _Linha(
            status=status,
            recebido_em=linha.recebido_em if linha else datetime.now(UTC),
            # Conclusao LIBERA o lease: o evento ja tem desfecho.
            processando_ate=None,
            dono=None,
            proxima_tentativa_em=linha.proxima_tentativa_em if linha else None,
            aviso_reservado_em=linha.aviso_reservado_em if linha else None,
            cnpj=linha.cnpj if linha else None,
        )

    async def contar_avisos(self, numero_pedido: str, desde: datetime) -> int:
        return sum(
            1
            for chave, linha in self._linhas.items()
            if chave.numero_pedido == numero_pedido
            and linha.recebido_em >= desde
            and linha.status in _CONTAM_COMO_AVISO
        )


class EventosPostgres:
    def __init__(self, fabrica: async_sessionmaker[AsyncSession]) -> None:
        self._fabrica = fabrica

    async def adquirir(
        self,
        chave: ChaveEvento,
        grupo: Grupo,
        *,
        cnpj: str | None = None,
        dono: str,
        lease_s: int,
        cooldown_s: int,
        desde: datetime,
        max_tentativas: int,
    ) -> Reserva:
        """Assume o evento com exclusividade, ou explica por que nao assumiu.

        Tudo acontece numa transacao so, sob `pg_advisory_xact_lock` do numero
        do pedido -- sem ele, decisoes concorrentes sobre o mesmo pedido leriam
        o estado ao mesmo tempo e todas passariam. O lock some com a transacao,
        que fecha aqui: nenhuma chamada HTTP acontece com ele na mao.

        Trata de exclusividade e CUSTO. Cota de mensagem e outra decisao, tomada
        depois da confirmacao -- ver `reservar_aviso`.
        """
        async with sessao(self._fabrica) as sess:
            # `hashtextextended` porque o numero e texto e o lock e por inteiro.
            await sess.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:p, 0))"),
                {"p": chave.numero_pedido},
            )

            existente = (
                await sess.execute(
                    select(
                        EventoFrete.status,
                        EventoFrete.processando_ate,
                        EventoFrete.proxima_tentativa_em,
                    ).where(*self._filtro(chave))
                )
            ).one_or_none()

            agora = datetime.now(UTC)
            if existente is not None:
                status_atual = StatusEvento(existente[0])
                if status_atual is not StatusEvento.PENDENTE:
                    return Reserva(status=status_atual)
                if existente[1] is not None and existente[1] > agora:
                    return Reserva(em_andamento=True)
                # COOLDOWN. Sem ele, repetir a mesma linha pendente reconsultava
                # a Frete Rapido a cada vez: o teto de tentativas so barra linha
                # NOVA, e concluir libera o lease imediatamente.
                if existente[2] is not None and existente[2] > agora:
                    return Reserva(em_espera=True)

            tentativas = await self._contar_tentativas(
                sess, chave.numero_pedido, desde
            )
            if existente is None and tentativas >= max_tentativas:
                return Reserva(custo_excedido=True, tentativas_recentes=tentativas)

            ate = agora + timedelta(seconds=lease_s)
            proxima = agora + timedelta(seconds=cooldown_s)
            if existente is None:
                await sess.execute(
                    pg_insert(EventoFrete)
                    .values(
                        numero_pedido=chave.numero_pedido,
                        codigo=chave.codigo,
                        data_ocorrencia=chave.data_ocorrencia,
                        grupo=grupo.value,
                        status=StatusEvento.PENDENTE.value,
                        tentativas=1,
                        cnpj=cnpj or None,
                        processando_ate=ate,
                        processando_por=dono,
                        proxima_tentativa_em=proxima,
                    )
                    .on_conflict_do_nothing(constraint="uq_evento_frete_ocorrencia")
                )
            else:
                await sess.execute(
                    update(EventoFrete)
                    .where(*self._filtro(chave))
                    .values(
                        tentativas=EventoFrete.tentativas + 1,
                        processando_ate=ate,
                        processando_por=dono,
                        proxima_tentativa_em=proxima,
                    )
                )
            return Reserva(adquirida=True)

    async def reservar_aviso(
        self,
        chave: ChaveEvento,
        *,
        desde: datetime,
        desde_volume: datetime,
        desde_hora: datetime,
        max_avisos: int,
        cnpj: str | None = None,
        max_global: int,
        max_cnpj: int,
    ) -> ReservaAviso:
        """Ocupa uma vaga de MENSAGEM, se houver.

        Chamado apos a confirmacao na fonte, de proposito: quando isto ficava
        junto da aquisicao do lease, tres eventos forjados simultaneos ocupavam
        as tres vagas antes de qualquer verificacao, e o evento legitimo que
        chegasse junto era descartado sem nunca ser consultado.

        Duas janelas, porque sao duas grandezas:

        - `desde_volume` (curta, ~1h) barra o MESMO codigo. Uma remessa de varias
          caixas emite uma ocorrencia por volume com minutos de diferenca, e isso
          e um unico fato para quem le.
        - `desde` (longa, ~6h) barra o excesso de avisos no pedido, qualquer que
          seja o codigo.

        Usar a janela longa para as duas coisas silenciava a segunda tentativa de
        entrega legitima do mesmo dia.
        """
        async with sessao(self._fabrica) as sess:
            await sess.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:p, 0))"),
                {"p": chave.numero_pedido},
            )

            ja_reservado = await sess.scalar(
                select(EventoFrete.aviso_reservado_em).where(*self._filtro(chave))
            )
            if ja_reservado is not None:
                # Reprocessamento da MESMA linha que ja tinha vaga: nao consome
                # outra, e nao pode ser barrada por si mesma.
                return ReservaAviso(concedida=True)

            repetido = await self._contar_reservados(
                sess, chave, desde_volume, codigo=chave.codigo
            )
            if repetido:
                return ReservaAviso(
                    cota_excedida=True, codigo_repetido=True, avisos_recentes=repetido
                )

            avisos = await self._contar_reservados(sess, chave, desde)
            if avisos >= max_avisos:
                return ReservaAviso(cota_excedida=True, avisos_recentes=avisos)

            # DISJUNTORES sistemicos. Ficam por ultimo: sao a rede de seguranca
            # de ordem de grandeza, nao a regra do dia a dia.
            #
            # Contagem aproximada. O advisory lock que seguramos e do PEDIDO;
            # cobrir a loja inteira exigiria um lock global, que serializaria o
            # webhook todo. Um disjuntor pode errar por alguns eventos sem perder
            # utilidade -- ele existe para pegar 10x, nao 1.02x.
            no_cnpj = await self._contar_na_hora(sess, desde_hora, cnpj=cnpj)
            if no_cnpj >= max_cnpj:
                return ReservaAviso(
                    limite_sistemico=f"CNPJ {cnpj or '(sem id)'}: {no_cnpj} avisos/h",
                    avisos_recentes=no_cnpj,
                )

            total = await self._contar_na_hora(sess, desde_hora)
            if total >= max_global:
                return ReservaAviso(
                    limite_sistemico=f"loja inteira: {total} avisos/h",
                    avisos_recentes=total,
                )

            await sess.execute(
                update(EventoFrete)
                .where(*self._filtro(chave))
                .values(aviso_reservado_em=datetime.now(UTC))
            )
            return ReservaAviso(concedida=True)

    async def _contar_na_hora(
        self, sess: AsyncSession, desde: datetime, cnpj: str | None = _SEM_FILTRO
    ) -> int:
        """Avisos reservados na janela, em TODOS os pedidos.

        Sem `cnpj` conta a loja inteira; com ele, so aquele CNPJ. O sentinela
        distingue "nao filtrar" de "filtrar por CNPJ nulo", que sao coisas
        diferentes -- eventos do segredo avulso tem `cnpj` nulo e precisam de
        teto proprio, nao de isencao.
        """
        filtros: list[ColumnElement[bool]] = [EventoFrete.aviso_reservado_em >= desde]
        if cnpj is not _SEM_FILTRO:
            filtros.append(
                EventoFrete.cnpj.is_(None) if cnpj is None else EventoFrete.cnpj == cnpj
            )

        total = await sess.scalar(
            select(func.count()).select_from(EventoFrete).where(*filtros)
        )
        return int(total or 0)

    async def renovar(self, chave: ChaveEvento, *, dono: str, lease_s: int) -> bool:
        """Estende o lease, se ainda somos donos. `False` = perdemos a posse.

        Chamado imediatamente antes do efeito externo. Estreita a janela entre
        "verifiquei que sou dono" e "enviei" para perto de zero -- sem isto, um
        lease vencido durante o processamento deixava dois workers enviarem.
        """
        async with sessao(self._fabrica) as sess:
            resultado = await sess.execute(
                update(EventoFrete)
                .where(
                    *self._filtro(chave),
                    EventoFrete.processando_por == dono,
                )
                .values(processando_ate=datetime.now(UTC) + timedelta(seconds=lease_s))
                .returning(EventoFrete.id)
            )
            return resultado.scalar_one_or_none() is not None

    async def _contar_reservados(
        self,
        sess: AsyncSession,
        chave: ChaveEvento,
        desde: datetime,
        codigo: int | None = None,
    ) -> int:
        """Vagas de mensagem ocupadas no pedido, EXCLUINDO a propria linha."""
        filtros: list[ColumnElement[bool]] = [
            EventoFrete.numero_pedido == chave.numero_pedido,
            EventoFrete.aviso_reservado_em >= desde,
            # A propria linha nao conta contra si mesma.
            not_(and_(*self._filtro(chave))),
        ]
        if codigo is not None:
            filtros.append(EventoFrete.codigo == codigo)

        total = await sess.scalar(
            select(func.count()).select_from(EventoFrete).where(*filtros)
        )
        return int(total or 0)

    async def _contar_avisos(
        self,
        sess: AsyncSession,
        numero_pedido: str,
        desde: datetime,
        agora: datetime,
        codigo: int | None = None,
    ) -> int:
        """Avisos que sairam ou vao sair. Inclui os EM VOO (lease vivo).

        Contar so os concluidos deixaria duas corridas simultaneas verem zero.
        Com `codigo`, restringe a um unico codigo de ocorrencia.
        """
        filtros: list[ColumnElement[bool]] = [
            EventoFrete.numero_pedido == numero_pedido,
            EventoFrete.recebido_em >= desde,
            or_(
                EventoFrete.status.in_([s.value for s in _CONTAM_COMO_AVISO]),
                and_(
                    EventoFrete.status == StatusEvento.PENDENTE.value,
                    EventoFrete.processando_ate > agora,
                ),
            ),
        ]
        if codigo is not None:
            filtros.append(EventoFrete.codigo == codigo)

        total = await sess.scalar(
            select(func.count()).select_from(EventoFrete).where(*filtros)
        )
        return int(total or 0)

    async def _contar_tentativas(
        self, sess: AsyncSession, numero_pedido: str, desde: datetime
    ) -> int:
        """Tudo que passou pelo gatilho, independente do desfecho.

        Existe para limitar CUSTO, nao mensagem: um evento que nunca confirma
        nao incrementa a cota de avisos e, sem este teto, custaria uma consulta
        a Frete Rapido a cada repeticao -- na mesma cota que a pagina usa.
        """
        total = await sess.scalar(
            select(func.count())
            .select_from(EventoFrete)
            .where(
                EventoFrete.numero_pedido == numero_pedido,
                EventoFrete.recebido_em >= desde,
                EventoFrete.status != StatusEvento.DESCARTADO.value,
            )
        )
        return int(total or 0)

    async def registrar(
        self,
        chave: ChaveEvento,
        grupo: Grupo,
        status: StatusEvento,
        erro: str | None = None,
        cnpj: str | None = None,
    ) -> None:
        """Grava um desfecho ja decidido, sem reservar. Idempotente."""
        async with sessao(self._fabrica) as sess:
            await sess.execute(
                pg_insert(EventoFrete)
                .values(
                    numero_pedido=chave.numero_pedido,
                    codigo=chave.codigo,
                    data_ocorrencia=chave.data_ocorrencia,
                    grupo=grupo.value,
                    status=status.value,
                    tentativas=1,
                    erro=erro,
                    cnpj=cnpj or None,
                )
                .on_conflict_do_nothing(constraint="uq_evento_frete_ocorrencia")
            )

    async def concluir(
        self,
        chave: ChaveEvento,
        status: StatusEvento,
        erro: str | None = None,
        *,
        dono: str | None = None,
    ) -> None:
        enviado_em = datetime.now(UTC) if status is StatusEvento.ENVIADO else None
        filtros = list(self._filtro(chave))
        if dono is not None:
            # FENCING. Sem isto, um worker cujo lease VENCEU ainda conseguia
            # concluir e apagar o lease de quem assumiu depois -- o novo dono
            # seguia trabalhando sobre uma linha ja sobrescrita e podia enviar de
            # novo. Escrita que fecha o evento exige posse comprovada.
            filtros.append(EventoFrete.processando_por == dono)

        async with sessao(self._fabrica) as sess:
            resultado = await sess.execute(
                update(EventoFrete)
                .where(*filtros)
                .values(
                    status=status.value,
                    erro=erro,
                    enviado_em=enviado_em,
                    # Concluir LIBERA o lease. Manter o lease vivo depois do
                    # desfecho travaria a proxima reentrega legitima do evento.
                    processando_ate=None,
                    processando_por=None,
                )
                .returning(EventoFrete.id)
            )
            if dono is not None and resultado.scalar_one_or_none() is None:
                logger.warning(
                    "conclusao ignorada: o lease do evento %s/%s ja pertence a "
                    "outro processo",
                    chave.numero_pedido,
                    chave.codigo,
                )

    async def contar_avisos(self, numero_pedido: str, desde: datetime) -> int:
        async with sessao(self._fabrica) as sess:
            total = await sess.scalar(
                select(func.count())
                .select_from(EventoFrete)
                .where(
                    EventoFrete.numero_pedido == numero_pedido,
                    EventoFrete.recebido_em >= desde,
                    EventoFrete.status.in_([s.value for s in _CONTAM_COMO_AVISO]),
                )
            )
        return int(total or 0)

    async def expurgar(self, dias: int) -> int:
        """Remove eventos alem da retencao. Roda junto com o expurgo LGPD.

        A tabela nao tem dado pessoal, entao isto e controle de VOLUME e nao
        exigencia legal: gravamos todo evento de todo pedido, o que cresce bem
        mais rapido que `consulta_log`. Ainda assim a politica precisa ser
        explicita -- tabela que so cresce e problema adiado, nao evitado.
        """
        limite = datetime.now(UTC) - timedelta(days=dias)
        async with sessao(self._fabrica) as sess:
            resultado = await sess.execute(
                delete(EventoFrete).where(EventoFrete.recebido_em < limite)
            )
        return int(getattr(resultado, "rowcount", 0) or 0)

    @staticmethod
    def _filtro(chave: ChaveEvento) -> tuple[ColumnElement[bool], ...]:
        # `IS NOT DISTINCT FROM` e nao `==`: com `data_ocorrencia` nula, um
        # `= NULL` nunca casa e o UPDATE nao atingiria linha alguma, deixando o
        # evento preso em `pendente` para sempre.
        return (
            EventoFrete.numero_pedido == chave.numero_pedido,
            EventoFrete.codigo == chave.codigo,
            EventoFrete.data_ocorrencia.is_not_distinct_from(chave.data_ocorrencia),
        )
