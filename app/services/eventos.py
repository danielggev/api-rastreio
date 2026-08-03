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

from sqlalchemy import ColumnElement, delete, func, select, update
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


class RepositorioEventos(Protocol):
    async def reservar(
        self, chave: ChaveEvento, grupo: Grupo, cnpj: str | None = None
    ) -> StatusEvento | None: ...

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


class EventosMemoria:
    """Implementacao em memoria. Usada nos testes e quando nao ha banco.

    Sem banco a deduplicacao so vale enquanto o processo viver -- uma reentrega
    da Frete Rapido depois de um reinicio passaria de novo. E uma degradacao
    aceita conscientemente: em producao `DATABASE_URL` e obrigatoria.
    """

    def __init__(self) -> None:
        self._linhas: dict[ChaveEvento, tuple[StatusEvento, datetime]] = {}

    async def reservar(
        self, chave: ChaveEvento, grupo: Grupo, cnpj: str | None = None
    ) -> StatusEvento | None:
        existente = self._linhas.get(chave)
        if existente is not None:
            return existente[0]
        self._linhas[chave] = (StatusEvento.PENDENTE, datetime.now(UTC))
        return None

    async def registrar(
        self,
        chave: ChaveEvento,
        grupo: Grupo,
        status: StatusEvento,
        erro: str | None = None,
        cnpj: str | None = None,
    ) -> None:
        self._linhas.setdefault(chave, (status, datetime.now(UTC)))

    async def concluir(
        self, chave: ChaveEvento, status: StatusEvento, erro: str | None = None
    ) -> None:
        recebido = self._linhas.get(chave, (status, datetime.now(UTC)))[1]
        self._linhas[chave] = (status, recebido)

    async def contar_avisos(self, numero_pedido: str, desde: datetime) -> int:
        return sum(
            1
            for chave, (status, recebido) in self._linhas.items()
            if chave.numero_pedido == numero_pedido
            and status in _CONTAM_COMO_AVISO
            and recebido >= desde
        )


class EventosPostgres:
    def __init__(self, fabrica: async_sessionmaker[AsyncSession]) -> None:
        self._fabrica = fabrica

    async def reservar(
        self, chave: ChaveEvento, grupo: Grupo, cnpj: str | None = None
    ) -> StatusEvento | None:
        """Reserva o evento. Devolve o status ja existente, ou `None` se e novo.

        `ON CONFLICT DO NOTHING` em vez de SELECT-depois-INSERT: a Frete Rapido
        pode entregar a mesma ocorrencia em paralelo, e a janela entre as duas
        consultas seria suficiente para o cliente receber a mensagem duas vezes.
        A restricao UNIQUE do banco e o unico arbitro confiavel.
        """
        async with sessao(self._fabrica) as sess:
            inserido = await sess.execute(
                pg_insert(EventoFrete)
                .values(
                    numero_pedido=chave.numero_pedido,
                    codigo=chave.codigo,
                    data_ocorrencia=chave.data_ocorrencia,
                    grupo=grupo.value,
                    status=StatusEvento.PENDENTE.value,
                    tentativas=1,
                    cnpj=cnpj or None,
                )
                .on_conflict_do_nothing(constraint="uq_evento_frete_ocorrencia")
                .returning(EventoFrete.id)
            )
            if inserido.scalar_one_or_none() is not None:
                return None

            # Ja existia. Uma reentrega da FR de um evento que ficou `pendente`
            # deve ser PROCESSADA de novo, nao ignorada -- por isso devolvemos o
            # status em vez de um booleano.
            atual = await sess.execute(
                update(EventoFrete)
                .where(*self._filtro(chave))
                .values(tentativas=EventoFrete.tentativas + 1)
                .returning(EventoFrete.status)
            )
            bruto = atual.scalar_one_or_none()
            return StatusEvento(bruto) if bruto else None

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
                .values(status=status.value, erro=erro, enviado_em=enviado_em)
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
