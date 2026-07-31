"""Rate limiting e descoberta do IP real atras do proxy.

O rate limiting e a defesa PRINCIPAL contra enumeracao de pedidos. Se ele
enxergar o IP errado, o controle se desliga sem emitir erro nenhum.
"""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers

from app.middleware.ip_cliente import e_confiavel, ip_do_cliente
from app.middleware.rate_limit import LimitadorJanelaDeslizante, interpretar_limite

PROXY = "127.0.0.1"
CLIENTE = "203.0.113.10"


class RequisicaoFalsa:
    def __init__(self, host: str | None, cabecalhos: dict[str, str] | None = None):
        self.client = type("C", (), {"host": host})() if host else None
        self.headers = Headers(cabecalhos or {})


def _req(host: str | None, **cabecalhos: str) -> RequisicaoFalsa:
    return RequisicaoFalsa(host, cabecalhos)


# --------------------------------------------------------------------------
# IP real atras do proxy
# --------------------------------------------------------------------------


def test_sem_proxy_usa_o_ip_da_conexao() -> None:
    req = _req(CLIENTE)
    assert ip_do_cliente(req, []) == CLIENTE  # type: ignore[arg-type]


def test_atras_de_proxy_confiavel_usa_o_forwarded_for() -> None:
    """TESTE CRITICO.

    Sem isto, `request.client.host` devolve 127.0.0.1 para TODOS os visitantes e
    o rate limit conta todo mundo no mesmo balde -- desligando o controle.
    """
    req = _req(PROXY, **{"x-forwarded-for": CLIENTE})
    assert ip_do_cliente(req, [PROXY]) == CLIENTE  # type: ignore[arg-type]


def test_forwarded_for_forjado_e_ignorado_sem_proxy_confiavel() -> None:
    """TESTE CRITICO.

    O cabecalho e enviado pelo cliente. Confiar nele sem lista de proxies
    permitiria forjar um IP por requisicao e escapar do limite completamente.
    """
    req = _req(CLIENTE, **{"x-forwarded-for": "1.2.3.4"})
    assert ip_do_cliente(req, []) == CLIENTE  # type: ignore[arg-type]


def test_forwarded_for_de_origem_nao_confiavel_e_ignorado() -> None:
    """Conexao direta de um IP qualquer nao pode se declarar proxy."""
    req = _req("198.51.100.5", **{"x-forwarded-for": "1.2.3.4"})
    assert ip_do_cliente(req, [PROXY]) == "198.51.100.5"  # type: ignore[arg-type]


def test_cadeia_de_proxies_usa_o_cliente_original() -> None:
    req = _req(PROXY, **{"x-forwarded-for": f"{CLIENTE}, 10.0.0.1, 10.0.0.2"})
    assert ip_do_cliente(req, [PROXY]) == CLIENTE  # type: ignore[arg-type]


def test_forwarded_for_invalido_cai_para_o_ip_da_conexao() -> None:
    req = _req(PROXY, **{"x-forwarded-for": "nao-e-ip"})
    assert ip_do_cliente(req, [PROXY]) == PROXY  # type: ignore[arg-type]


def test_ipv6_e_aceito() -> None:
    req = _req(PROXY, **{"x-forwarded-for": "2001:db8::1"})
    assert ip_do_cliente(req, [PROXY]) == "2001:db8::1"  # type: ignore[arg-type]


def test_sem_cliente_identificavel() -> None:
    assert ip_do_cliente(_req(None), [PROXY]) == "desconhecido"  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Faixas CIDR -- necessarias por causa do IP dinamico do container do proxy
# --------------------------------------------------------------------------


def test_faixa_cidr_reconhece_o_proxy() -> None:
    """No Docker o proxy recebe IP dinamico; exigir IP exato quebraria sempre."""
    req = _req("172.18.0.5", **{"x-forwarded-for": CLIENTE})
    assert ip_do_cliente(req, ["172.16.0.0/12"]) == CLIENTE  # type: ignore[arg-type]


def test_fora_da_faixa_nao_e_confiavel() -> None:
    req = _req("203.0.113.99", **{"x-forwarded-for": "1.2.3.4"})
    assert ip_do_cliente(req, ["172.16.0.0/12"]) == "203.0.113.99"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("ip", "entradas", "esperado"),
    [
        ("127.0.0.1", ["127.0.0.1"], True),
        ("172.18.0.5", ["172.16.0.0/12"], True),
        ("10.0.0.7", ["10.0.0.0/8", "127.0.0.1"], True),
        ("203.0.113.1", ["172.16.0.0/12"], False),
        ("203.0.113.1", [], False),
        ("", ["127.0.0.1"], False),
        ("nao-e-ip", ["127.0.0.1"], False),
    ],
)
def test_e_confiavel(ip: str, entradas: list[str], esperado: bool) -> None:
    assert e_confiavel(ip, entradas) is esperado


def test_entrada_malformada_nao_derruba_a_requisicao() -> None:
    """Erro de digitacao no .env nao pode virar erro 500 para o cliente."""
    assert e_confiavel("127.0.0.1", ["nao-e-cidr", "127.0.0.1"]) is True
    assert e_confiavel("203.0.113.1", ["///"]) is False


# --------------------------------------------------------------------------
# Janela deslizante
# --------------------------------------------------------------------------


def test_permite_ate_o_limite_e_bloqueia_depois() -> None:
    limitador = LimitadorJanelaDeslizante(limite=3, janela_s=60)

    assert [limitador.permitir("ip", agora=100.0) for _ in range(3)] == [True] * 3
    assert not limitador.permitir("ip", agora=100.0)


def test_contagem_e_por_chave() -> None:
    """Um cliente abusivo nao pode bloquear os demais."""
    limitador = LimitadorJanelaDeslizante(limite=2, janela_s=60)

    assert limitador.permitir("ip-a", agora=100.0)
    assert limitador.permitir("ip-a", agora=100.0)
    assert not limitador.permitir("ip-a", agora=100.0)
    assert limitador.permitir("ip-b", agora=100.0)


def test_janela_desliza_liberando_novas_requisicoes() -> None:
    limitador = LimitadorJanelaDeslizante(limite=2, janela_s=60)

    assert limitador.permitir("ip", agora=100.0)
    assert limitador.permitir("ip", agora=100.0)
    assert not limitador.permitir("ip", agora=130.0)
    # Passados 60s da primeira, ela sai da janela.
    assert limitador.permitir("ip", agora=161.0)


def test_restantes() -> None:
    limitador = LimitadorJanelaDeslizante(limite=3, janela_s=60)
    assert limitador.restantes("ip", agora=100.0) == 3
    limitador.permitir("ip", agora=100.0)
    assert limitador.restantes("ip", agora=100.0) == 2


def test_limpeza_evita_crescimento_indefinido_de_memoria() -> None:
    limitador = LimitadorJanelaDeslizante(limite=5, janela_s=60)
    for i in range(100):
        limitador.permitir(f"ip-{i}", agora=100.0)

    assert limitador.limpar(agora=1000.0) == 100
    assert limitador.restantes("ip-0", agora=1000.0) == 5


# --------------------------------------------------------------------------
# Configuracao
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("10/minute", (10, 60)),
        ("5/second", (5, 1)),
        ("100/hour", (100, 3600)),
        (" 10 / MINUTE ", (10, 60)),
    ],
)
def test_interpretar_limite(texto: str, esperado: tuple[int, int]) -> None:
    assert interpretar_limite(texto) == esperado


@pytest.mark.parametrize("texto", ["10", "10/decade", "abc/minute", ""])
def test_limite_invalido_falha_no_boot(texto: str) -> None:
    """Melhor nao subir do que subir sem o controle principal de seguranca."""
    with pytest.raises(ValueError, match="RATE_LIMIT"):
        interpretar_limite(texto)
