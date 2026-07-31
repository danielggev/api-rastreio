"""Modo demonstracao.

O risco central aqui e duplo: os fakes ficarem incoerentes entre si, e o modo
vazar para producao servindo dados falsos a clientes reais.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.schemas import Grupo, Resultado
from app.services.cache import CacheDesligado
from app.services.consulta import ServicoConsulta
from app.services.demo import CENARIOS, FreteRapidoDemo, ShopifyDemo, catalogo

EMAIL = "demo@exemplo.com"
LOJA = {"prefixo": "", "sufixo": ""}


def _servico() -> ServicoConsulta:
    return ServicoConsulta(
        ShopifyDemo(EMAIL),  # type: ignore[arg-type]
        FreteRapidoDemo(),  # type: ignore[arg-type]
        CacheDesligado(),
    )


# --------------------------------------------------------------------------
# Trava de producao
# --------------------------------------------------------------------------


def test_demo_mode_e_proibido_em_producao() -> None:
    """Um deploy distraido com DEMO_MODE ligado serviria dados falsos.

    A aplicacao precisa recusar-se a subir, nao apenas avisar.
    """
    with pytest.raises(ValueError, match="DEMO_MODE"):
        Settings(env="production", demo_mode=True)


def test_demo_mode_dispensa_credenciais_reais() -> None:
    """Sem integracao real, exigir tokens seria burocracia inutil."""
    s = Settings(
        env="development",
        demo_mode=True,
        shopify_shop_domain="",
        shopify_client_id="",
        shopify_client_secret="",
        email_hmac_key="",
    )
    assert s.demo_mode


# --------------------------------------------------------------------------
# Coerencia entre os dois fakes
# --------------------------------------------------------------------------


async def test_todos_os_cenarios_respondem_sem_quebrar() -> None:
    servico = _servico()
    for numero in CENARIOS:
        consulta = await servico.consultar(EMAIL, numero)
        assert consulta.resultado in set(Resultado), numero


@pytest.mark.parametrize(
    ("numero", "grupo"),
    [
        ("900001", Grupo.ENTREGUE),
        ("900002", Grupo.EM_TRANSITO),
        ("900003", Grupo.SAIU_PARA_ENTREGA),
        ("900004", Grupo.TENTATIVA_FALHA),
        ("900005", Grupo.AGUARDANDO_RETIRADA),
        ("900006", Grupo.DEVOLUCAO),
    ],
)
async def test_cada_cenario_produz_o_grupo_esperado(
    numero: str, grupo: Grupo
) -> None:
    """A interface precisa de um caso por grupo para ser construida."""
    consulta = await _servico().consultar(EMAIL, numero)
    assert consulta.resultado is Resultado.SUCESSO
    assert consulta.resposta.status_atual.grupo is grupo  # type: ignore[union-attr]


async def test_cenario_com_campos_nulos() -> None:
    """A UI nao pode assumir presenca de transportadora nem de previsao."""
    consulta = await _servico().consultar(EMAIL, "900007")
    assert consulta.resultado is Resultado.SUCESSO
    assert consulta.resposta.transportadora is None  # type: ignore[union-attr]
    assert consulta.resposta.previsao_entrega is None  # type: ignore[union-attr]


async def test_sem_fulfillment_mas_com_ocorrencias() -> None:
    """Caso REAL dos pedidos de teste: ha ocorrencias antes do despacho."""
    consulta = await _servico().consultar(EMAIL, "900008")
    assert consulta.resultado is Resultado.SUCESSO
    assert consulta.resposta.status_atual.grupo is Grupo.PREPARANDO  # type: ignore[union-attr]


async def test_sem_rastreio() -> None:
    consulta = await _servico().consultar(EMAIL, "900009")
    assert consulta.resultado is Resultado.SEM_RASTREIO


async def test_vazio_fr() -> None:
    consulta = await _servico().consultar(EMAIL, "900010")
    assert consulta.resultado is Resultado.VAZIO_FR


async def test_entrega_atrasada() -> None:
    consulta = await _servico().consultar(EMAIL, "900011")
    assert consulta.resultado is Resultado.SUCESSO
    assert consulta.resposta.entrega_atrasada is True  # type: ignore[union-attr]


async def test_entregue_com_atraso_nao_e_marcado_como_atrasado() -> None:
    consulta = await _servico().consultar(EMAIL, "900001")
    assert consulta.resposta.entrega_atrasada is False  # type: ignore[union-attr]


async def test_erro_externo() -> None:
    """Falha sem dados nunca pode virar "sem rastreio"."""
    consulta = await _servico().consultar(EMAIL, "900012")
    assert consulta.resultado is Resultado.ERRO_EXTERNO
    assert consulta.status_http == 503


async def test_numero_desconhecido_e_nao_encontrado() -> None:
    consulta = await _servico().consultar(EMAIL, "123456")
    assert consulta.resultado is Resultado.NAO_ENCONTRADO


async def test_email_errado_e_rejeitado_tambem_no_demo() -> None:
    """A validacao de email continua valendo -- o demo nao afrouxa a seguranca."""
    consulta = await _servico().consultar("outro@exemplo.com", "900001")
    assert consulta.resultado is Resultado.NAO_ENCONTRADO


async def test_historico_esta_em_ordem_decrescente() -> None:
    consulta = await _servico().consultar(EMAIL, "900001")
    datas = [e.data for e in consulta.resposta.historico]  # type: ignore[union-attr]
    assert datas == sorted(datas, reverse=True)


async def test_datas_sao_relativas_a_hoje() -> None:
    """Sem isso, a demonstracao envelheceria e "atrasado" pararia de fazer sentido."""
    from datetime import UTC, datetime

    consulta = await _servico().consultar(EMAIL, "900003")
    mais_recente = consulta.resposta.status_atual.data  # type: ignore[union-attr]
    assert mais_recente is not None
    idade = datetime.now(UTC) - mais_recente
    assert idade.days <= 1


# --------------------------------------------------------------------------
# Endpoint de descoberta
# --------------------------------------------------------------------------


def test_catalogo_lista_todos_os_cenarios() -> None:
    assert len(catalogo()) == len(CENARIOS)
    assert all(item["numero"] and item["descricao"] for item in catalogo())


def test_endpoint_de_demo_existe_apenas_no_modo_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings
    from app.main import criar_app

    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    with TestClient(criar_app()) as c:
        resposta = c.get("/api/v1/demo")
    assert resposta.status_code == 200
    assert resposta.json()["pedidos"]

    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    with TestClient(criar_app()) as c:
        assert c.get("/api/v1/demo").status_code == 404
