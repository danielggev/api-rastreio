"""LGPD: pseudonimizacao e retencao.

Contem o ultimo dos nove testes obrigatorios do plano -- garantir que nenhum
email seja gravado em claro no banco.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base, ConsultaLog
from app.schemas import Anomalia, RespostaErro, Resultado
from app.services.auditoria import Auditoria
from app.services.consulta import Consulta
from app.services.pseudonimo import hmac_email

EMAIL = "Cliente@Exemplo.com.BR"
CHAVE = "chave-de-teste"


@pytest_asyncio.fixture
async def fabrica() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _consulta(resultado: Resultado = Resultado.SUCESSO) -> Consulta:
    return Consulta(
        resposta=RespostaErro(resultado=Resultado.NAO_ENCONTRADO, mensagem="x"),
        resultado=resultado,
        anomalias=[Anomalia.TAG_CNPJ_AUSENTE],
        cnpj="melhores",
    )


# --------------------------------------------------------------------------
# HMAC
# --------------------------------------------------------------------------


def test_hmac_e_estavel_para_formas_equivalentes() -> None:
    """Comparacao e HMAC usam a MESMA normalizacao.

    Se divergissem, o mesmo email geraria hashes diferentes e a correlacao dos
    logs quebraria sem sintoma visivel.
    """
    formas = ["Cliente@Exemplo.com", " cliente@exemplo.com ", "CLIENTE@EXEMPLO.COM"]
    assert len({hmac_email(f, CHAVE) for f in formas}) == 1


def test_hmac_muda_com_a_chave() -> None:
    assert hmac_email(EMAIL, "chave-a") != hmac_email(EMAIL, "chave-b")


def test_hmac_nao_contem_o_email() -> None:
    digest = hmac_email(EMAIL, CHAVE)
    assert digest is not None
    assert "cliente" not in digest.lower()
    assert "exemplo" not in digest.lower()
    assert len(digest) == 64  # SHA-256 em hexadecimal


def test_hmac_de_email_ausente() -> None:
    assert hmac_email(None, CHAVE) is None
    assert hmac_email("", CHAVE) is None


def test_hmac_exige_chave_configurada() -> None:
    with pytest.raises(ValueError, match="EMAIL_HMAC_KEY"):
        hmac_email(EMAIL, "")


# --------------------------------------------------------------------------
# Auditoria
# --------------------------------------------------------------------------


async def test_nenhum_email_em_claro_no_banco(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """TESTE OBRIGATORIO do plano (o nono).

    Varre TODAS as colunas do registro procurando qualquer vestigio do email --
    nao apenas a coluna onde ele deveria estar. Assim uma alteracao futura que
    grave o email noutro campo tambem falha aqui.
    """
    auditoria = Auditoria(fabrica)
    await auditoria.registrar(
        _consulta(),
        email=EMAIL,
        numero_pedido="59552",
        ip="203.0.113.10",
        user_agent="Mozilla/5.0",
        latencia_ms=120,
    )

    async with fabrica() as sess:
        registros = list((await sess.execute(select(ConsultaLog))).scalars())

    assert len(registros) == 1
    despejo = json.dumps(
        {c.name: str(getattr(registros[0], c.name)) for c in ConsultaLog.__table__.columns}
    ).lower()

    assert "cliente@exemplo.com.br" not in despejo
    assert "cliente" not in despejo
    assert "exemplo.com" not in despejo
    # O que deve estar la e o HMAC.
    assert registros[0].email_hmac == hmac_email(EMAIL)


async def test_registro_guarda_metadados_de_auditoria(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    auditoria = Auditoria(fabrica)
    await auditoria.registrar(
        _consulta(Resultado.VAZIO_FR),
        email=EMAIL,
        numero_pedido="59552",
        ip="203.0.113.10",
        user_agent="Mozilla/5.0",
        latencia_ms=88,
    )

    async with fabrica() as sess:
        registro = (await sess.execute(select(ConsultaLog))).scalar_one()

    assert registro.resultado == "vazio_fr"
    assert registro.numero_pedido == "59552"
    assert registro.cnpj == "melhores"
    assert registro.anomalias == ["tag_cnpj_ausente"]
    assert registro.latencia_ms == 88


async def test_user_agent_longo_e_truncado(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """Cabecalho controlado pelo cliente nao pode inchar o banco sem teto."""
    auditoria = Auditoria(fabrica)
    await auditoria.registrar(
        _consulta(),
        email=EMAIL,
        numero_pedido="59552",
        ip="203.0.113.10",
        user_agent="A" * 5000,
        latencia_ms=1,
    )

    async with fabrica() as sess:
        registro = (await sess.execute(select(ConsultaLog))).scalar_one()

    assert registro.user_agent is not None
    assert len(registro.user_agent) <= 512


async def test_falha_de_auditoria_nao_derruba_a_consulta() -> None:
    """O cliente nao pode ficar sem resposta porque o banco caiu."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    fabrica_sem_tabelas = async_sessionmaker(engine)

    auditoria = Auditoria(fabrica_sem_tabelas)
    # Nao levanta, apesar de a tabela nao existir.
    await auditoria.registrar(
        _consulta(), email=EMAIL, numero_pedido="1", ip="1.1.1.1", user_agent=None
    )
    await engine.dispose()


async def test_sem_banco_a_auditoria_e_silenciosa() -> None:
    auditoria = Auditoria(None)
    await auditoria.registrar(
        _consulta(), email=EMAIL, numero_pedido="1", ip="1.1.1.1", user_agent=None
    )
    assert await auditoria.expurgar() == 0


# --------------------------------------------------------------------------
# Retencao
# --------------------------------------------------------------------------


async def test_expurgo_remove_apenas_registros_alem_da_retencao(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    agora = datetime.now(UTC)
    async with fabrica() as sess:
        sess.add_all(
            [
                ConsultaLog(
                    criado_em=agora - timedelta(days=120),
                    resultado="sucesso",
                    email_hmac="antigo",
                ),
                ConsultaLog(
                    criado_em=agora - timedelta(days=30),
                    resultado="sucesso",
                    email_hmac="recente",
                ),
            ]
        )
        await sess.commit()

    auditoria = Auditoria(fabrica)
    assert await auditoria.expurgar(dias=90) == 1

    async with fabrica() as sess:
        restantes = list((await sess.execute(select(ConsultaLog))).scalars())

    assert [r.email_hmac for r in restantes] == ["recente"]


async def test_taxa_alimenta_o_alerta_agregado(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """Alerta e por TAXA, nao por evento: `vazio_fr` isolado e normal."""
    agora = datetime.now(UTC)
    async with fabrica() as sess:
        sess.add_all(
            [ConsultaLog(criado_em=agora, resultado="vazio_fr") for _ in range(3)]
            + [ConsultaLog(criado_em=agora, resultado="sucesso") for _ in range(7)]
        )
        await sess.commit()

    alvo, total = await Auditoria(fabrica).taxa_de("vazio_fr", janela_horas=1)
    assert (alvo, total) == (3, 10)
