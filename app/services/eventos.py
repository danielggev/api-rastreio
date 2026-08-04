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
    """Resultado de tentar assumir um evento.

    Exatamente UM dos campos manda:

    - `adquirida` -- somos donos do lease; siga e produza os efeitos externos.
    - `status` -- ja havia desfecho terminal gravado (reentrega da Frete Rapido).
    - `em_andamento` -- outro processo tem o lease vivo. Responder 200 e nao
      duplicar: quem esta com ele conclui.
    - `cota_excedida` -- a trava anti-spam barrou antes de qualquer efeito.
    """

    adquirida: bool = False
    status: StatusEvento | None = None
    em_andamento: bool = False
    cota_excedida: bool = False
    avisos_recentes: int = 0
    tentativas_recentes: int = 0


class RepositorioEventos(Protocol):
    async def adquirir(
        self,
        chave: ChaveEvento,
        grupo: Grupo,
        *,
        cnpj: str | None = None,
        lease_s: int,
        desde: datetime,
        max_avisos: int,
        max_tentativas: int,
    ) -> Reserva: ...

    async def registrar(
        self,
        chave: ChaveEvento,
        grupo: Grupo,
        status: StatusEvento,
        erro: str | None = None,
        cnpj: str | None = None,
    ) -> None: ...

    async def concluir(
        self, chave: ChaveEvento, status: StatusEvento, erro: str | None = None
    ) -> None: ...

    async def contar_avisos(self, numero_pedido: str, desde: datetime) -> int: ...


@dataclass
class _Linha:
    status: StatusEvento
    recebido_em: datetime
    processando_ate: datetime | None = None


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
        lease_s: int,
        desde: datetime,
        max_avisos: int,
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

        avisos = self._contar(chave.numero_pedido, desde, _CONTAM_COMO_AVISO, agora)
        tentativas = self._contar(chave.numero_pedido, desde, None, agora)

        if linha is None and tentativas >= max_tentativas:
            return Reserva(cota_excedida=True, tentativas_recentes=tentativas)
        if avisos >= max_avisos:
            return Reserva(cota_excedida=True, avisos_recentes=avisos)

        self._linhas[chave] = _Linha(
            status=StatusEvento.PENDENTE,
            recebido_em=linha.recebido_em if linha else agora,
            processando_ate=agora + timedelta(seconds=lease_s),
        )
        return Reserva(adquirida=True)

    def _contar(
        self,
        numero_pedido: str,
        desde: datetime,
        estados: tuple[StatusEvento, ...] | None,
        agora: datetime,
    ) -> int:
        total = 0
        for chave, linha in self._linhas.items():
            if chave.numero_pedido != numero_pedido or linha.recebido_em < desde:
                continue
            if estados is None:
                # Tentativas: tudo que passou pelo gatilho.
                if linha.status is not StatusEvento.DESCARTADO:
                    total += 1
            elif linha.status in estados:
                total += 1
            elif (
                linha.status is StatusEvento.PENDENTE
                and linha.processando_ate
                and linha.processando_ate > agora
            ):
                # Em voo conta como aviso: senao duas corridas simultaneas
                # veriam zero e as duas enviariam.
                total += 1
        return total

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
        self, chave: ChaveEvento, status: StatusEvento, erro: str | None = None
    ) -> None:
        linha = self._linhas.get(chave)
        self._linhas[chave] = _Linha(
            status=status,
            recebido_em=linha.recebido_em if linha else datetime.now(UTC),
            # Conclusao LIBERA o lease: o evento ja tem desfecho.
            processando_ate=None,
        )

    async def contar_avisos(self, numero_pedido: str, desde: datetime) -> int:
        return self._contar(
            numero_pedido, desde, _CONTAM_COMO_AVISO, datetime.now(UTC)
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
        lease_s: int,
        desde: datetime,
        max_avisos: int,
        max_tentativas: int,
    ) -> Reserva:
        """Assume o evento com exclusividade, ou explica por que nao assumiu.

        Tres coisas acontecem numa transacao so, e e isso que as torna corretas:

        1. **Lock consultivo por PEDIDO.** Sem ele, dois eventos diferentes do
           mesmo pedido contariam a cota ao mesmo tempo, veriam zero, e os dois
           enviariam. `pg_advisory_xact_lock` serializa por numero de pedido e
           some junto com a transacao -- nao ha o que liberar a mao.
        2. **Contagem da cota**, ja sob o lock.
        3. **Lease.** `ON CONFLICT DO NOTHING` arbitra quem cria a LINHA; o
           lease arbitra quem executa o EFEITO. Eram coisas diferentes e o
           codigo tratava como uma so.
        """
        async with sessao(self._fabrica) as sess:
            # `hashtextextended` porque o numero e texto e o lock e por inteiro.
            await sess.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:p, 0))"),
                {"p": chave.numero_pedido},
            )

            existente = (
                await sess.execute(
                    select(EventoFrete.status, EventoFrete.processando_ate).where(
                        *self._filtro(chave)
                    )
                )
            ).one_or_none()

            agora = datetime.now(UTC)
            if existente is not None:
                status_atual = StatusEvento(existente[0])
                if status_atual is not StatusEvento.PENDENTE:
                    return Reserva(status=status_atual)
                if existente[1] is not None and existente[1] > agora:
                    return Reserva(em_andamento=True)

            avisos = await self._contar_avisos(sess, chave.numero_pedido, desde, agora)
            tentativas = await self._contar_tentativas(
                sess, chave.numero_pedido, desde
            )

            # O teto de TENTATIVAS so barra evento novo: um ja registrado
            # precisa poder ser reprocessado ate concluir.
            if existente is None and tentativas >= max_tentativas:
                return Reserva(cota_excedida=True, tentativas_recentes=tentativas)
            if avisos >= max_avisos:
                return Reserva(cota_excedida=True, avisos_recentes=avisos)

            ate = agora + timedelta(seconds=lease_s)
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
                    )
                    .on_conflict_do_nothing(constraint="uq_evento_frete_ocorrencia")
                )
            else:
                await sess.execute(
                    update(EventoFrete)
                    .where(*self._filtro(chave))
                    .values(
                        tentativas=EventoFrete.tentativas + 1, processando_ate=ate
                    )
                )
            return Reserva(adquirida=True)

    async def _contar_avisos(
        self, sess: AsyncSession, numero_pedido: str, desde: datetime, agora: datetime
    ) -> int:
        """Avisos que sairam ou vao sair. Inclui os EM VOO (lease vivo).

        Contar so os concluidos deixaria duas corridas simultaneas verem zero.
        """
        total = await sess.scalar(
            select(func.count())
            .select_from(EventoFrete)
            .where(
                EventoFrete.numero_pedido == numero_pedido,
                EventoFrete.recebido_em >= desde,
                or_(
                    EventoFrete.status.in_([s.value for s in _CONTAM_COMO_AVISO]),
                    and_(
                        EventoFrete.status == StatusEvento.PENDENTE.value,
                        EventoFrete.processando_ate > agora,
                    ),
                ),
            )
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
        self, chave: ChaveEvento, status: StatusEvento, erro: str | None = None
    ) -> None:
        enviado_em = datetime.now(UTC) if status is StatusEvento.ENVIADO else None
        async with sessao(self._fabrica) as sess:
            await sess.execute(
                update(EventoFrete)
                .where(*self._filtro(chave))
                .values(
                    status=status.value,
                    erro=erro,
                    enviado_em=enviado_em,
                    # Concluir LIBERA o lease. Manter o lease vivo depois do
                    # desfecho travaria a proxima reentrega legitima do evento.
                    processando_ate=None,
                )
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
