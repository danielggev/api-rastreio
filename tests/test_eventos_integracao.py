"""Concorrencia REAL contra Postgres -- a lacuna apontada na revisao.

Os testes de `test_webhook_fr.py` usam `EventosMemoria`, onde `adquirir` nao tem
`await` interno e portanto e atomico por construcao. Eles provam a logica do
SERVICO, nao o SQL: o `pg_advisory_xact_lock`, o `ON CONFLICT` e o fencing nunca
sao exercitados por eles.

Sem isto, "duas sessoes independentes nao enviam duas vezes" e afirmacao, nao
fato verificado.

    # Postgres descartavel
    docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=teste \\
      -e POSTGRES_DB=rastreio_teste --name pg-teste postgres:17-alpine

    set DATABASE_URL_TESTE=postgresql+psycopg://postgres:teste@localhost:55432/rastreio_teste
    .venv\\Scripts\\python.exe -m pytest tests/test_eventos_integracao.py -v

Fora do CI de proposito: exige infraestrutura. Rode antes de ligar o envio.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Base
from app.db.session import criar_engine, criar_fabrica
from app.schemas import Grupo, StatusEvento
from app.services.eventos import ChaveEvento, EventosPostgres

URL = os.environ.get("DATABASE_URL_TESTE", "")

pytestmark = [
    pytest.mark.integracao,
    pytest.mark.skipif(not URL, reason="defina DATABASE_URL_TESTE"),
]

COMUM = {"lease_s": 120, "cooldown_s": 45, "max_tentativas": 20}


@pytest.fixture
async def fabrica() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = criar_engine(URL)
    async with engine.begin() as conexao:
        await conexao.run_sync(Base.metadata.drop_all)
        await conexao.run_sync(Base.metadata.create_all)
    yield criar_fabrica(engine)
    await engine.dispose()


def _chave(codigo: int = 232, data: datetime | None = None) -> ChaveEvento:
    return ChaveEvento("59552", codigo, data)


def _desde() -> datetime:
    return datetime.now(UTC) - timedelta(hours=6)


async def test_so_um_adquire_o_mesmo_evento(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """O caso que `EventosMemoria` nao consegue provar.

    Oito corrotinas, sessoes independentes, mesmo evento. Quem arbitra e o
    `ON CONFLICT` mais o lease -- nao a ausencia de `await`.
    """
    eventos = EventosPostgres(fabrica)
    chave = _chave()

    reservas = await asyncio.gather(
        *(
            eventos.adquirir(
                chave, Grupo.AGUARDANDO_RETIRADA, dono=f"w{i}", desde=_desde(), **COMUM
            )
            for i in range(8)
        )
    )

    assert sum(1 for r in reservas if r.adquirida) == 1
    assert sum(1 for r in reservas if r.em_andamento or r.em_espera) == 7


async def test_cota_de_aviso_nao_e_furada_por_corrida(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """Cinco eventos DIFERENTES do mesmo pedido, ao mesmo tempo, com teto 2.

    Sem o `pg_advisory_xact_lock`, todos leriam zero e todos reservariam.
    """
    eventos = EventosPostgres(fabrica)
    chaves = [
        _chave(codigo=32, data=datetime(2026, 8, d, 9, 0, tzinfo=UTC))
        for d in range(1, 6)
    ]
    for chave in chaves:
        await eventos.adquirir(
            chave, Grupo.TENTATIVA_FALHA, dono="w", desde=_desde(), **COMUM
        )

    vagas = await asyncio.gather(
        *(
            eventos.reservar_aviso(
                chave,
                desde=_desde(),
                desde_volume=datetime.now(UTC) - timedelta(hours=6),
                max_avisos=2,
            )
            for chave in chaves
        )
    )

    assert sum(1 for v in vagas if v.concedida) <= 2


async def test_fencing_recusa_conclusao_de_dono_antigo(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """Lease vencido de A, B assume, A tenta concluir atrasado."""
    eventos = EventosPostgres(fabrica)
    chave = _chave()

    await eventos.adquirir(
        chave,
        Grupo.AGUARDANDO_RETIRADA,
        dono="A",
        desde=_desde(),
        lease_s=1,
        cooldown_s=1,
        max_tentativas=20,
    )
    await asyncio.sleep(1.2)
    reserva_b = await eventos.adquirir(
        chave, Grupo.AGUARDANDO_RETIRADA, dono="B", desde=_desde(), **COMUM
    )
    assert reserva_b.adquirida

    # A, atrasado, tenta fechar o evento em nome de ninguem.
    await eventos.concluir(chave, StatusEvento.ENVIADO, dono="A")

    assert await eventos.renovar(chave, dono="B", lease_s=120) is True
    assert await eventos.renovar(chave, dono="A", lease_s=120) is False


async def test_cooldown_bloqueia_reprocessamento_imediato(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """A amplificacao 1-para-1: sem cooldown, cada repeticao reconsulta a FR."""
    eventos = EventosPostgres(fabrica)
    chave = _chave()

    await eventos.adquirir(
        chave, Grupo.AGUARDANDO_RETIRADA, dono="A", desde=_desde(), **COMUM
    )
    await eventos.concluir(chave, StatusEvento.PENDENTE, "falhou", dono="A")

    # Lease liberado, mas o cooldown segue valendo.
    repetida = await eventos.adquirir(
        chave, Grupo.AGUARDANDO_RETIRADA, dono="B", desde=_desde(), **COMUM
    )

    assert repetida.em_espera
    assert not repetida.adquirida


async def test_dedup_com_data_nula_usa_NULLS_NOT_DISTINCT(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """No Postgres, NULL e distinto de NULL num indice unico por PADRAO.

    Sem a clausula, dois eventos sem data criariam duas linhas e o cliente
    receberia a mensagem duas vezes. Isto so da para verificar com banco real.
    """
    eventos = EventosPostgres(fabrica)
    chave = _chave(data=None)

    primeira = await eventos.adquirir(
        chave, Grupo.AGUARDANDO_RETIRADA, dono="A", desde=_desde(), **COMUM
    )
    await eventos.concluir(chave, StatusEvento.ENVIADO, dono="A")
    segunda = await eventos.adquirir(
        chave, Grupo.AGUARDANDO_RETIRADA, dono="B", desde=_desde(), **COMUM
    )

    assert primeira.adquirida
    assert segunda.status is StatusEvento.ENVIADO
