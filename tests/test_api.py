"""Testes da rota HTTP: status, cabecalhos e contrato discriminado."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import criar_app
from app.middleware.rate_limit import LimitadorJanelaDeslizante
from app.schemas import OcorrenciaFR, Resultado
from app.services.consulta import ServicoConsulta
from app.services.multi_cnpj import ResultadoBusca
from app.services.normalizacao import NumeroPedidoFR
from app.services.ordenacao import indexar
from app.services.shopify import PedidoShopify

EMAIL = "cliente@exemplo.com"


class ShopifyFalso:
    def __init__(self, pedido: PedidoShopify | None) -> None:
        self.pedido = pedido

    async def buscar_pedido(self, numero: NumeroPedidoFR) -> PedidoShopify | None:
        return self.pedido


class FreteRapidoFalso:
    """Substitui o `BuscadorMultiCNPJ`."""

    def __init__(self, ocorrencias: list[OcorrenciaFR]) -> None:
        self.ocorrencias = ocorrencias

    async def buscar(
        self, numero: NumeroPedidoFR, tags: list[str] | None = None
    ) -> ResultadoBusca:
        return ResultadoBusca(
            ocorrencias=list(self.ocorrencias),
            cnpj="empresa-a" if self.ocorrencias else None,
        )


def _pedido(**kw: object) -> PedidoShopify:
    base = {
        "id": "gid://shopify/Order/1",
        "name": "#59552",
        "email_normalizado": EMAIL,
        "criado_em": None,
        "tem_fulfillment": False,
        "codigo_rastreio": None,
    }
    base.update(kw)
    return PedidoShopify(**base)  # type: ignore[arg-type]


def _ocorrencias() -> list[OcorrenciaFR]:
    return indexar(
        [
            OcorrenciaFR(codigo=0, nome="Contratado"),
            OcorrenciaFR(codigo=1, nome="Aguardando coleta / postagem"),
        ]
    )


def _cliente(pedido: PedidoShopify | None, ocorrencias: list[OcorrenciaFR]) -> TestClient:
    app = criar_app()
    app.state.servico_consulta = ServicoConsulta(
        ShopifyFalso(pedido),  # type: ignore[arg-type]
        FreteRapidoFalso(ocorrencias),  # type: ignore[arg-type]
    )
    return TestClient(app)


@pytest.fixture
def cliente_sucesso() -> Iterator[TestClient]:
    with _cliente(_pedido(), _ocorrencias()) as c:
        yield c


def test_sucesso_devolve_200_e_contrato_completo(cliente_sucesso: TestClient) -> None:
    r = cliente_sucesso.post(
        "/api/v1/rastreio", json={"email": EMAIL, "numero_pedido": "59552"}
    )

    assert r.status_code == 200
    corpo = r.json()
    assert corpo["resultado"] == Resultado.SUCESSO
    assert corpo["pedido"] == "#59552"
    assert corpo["status_atual"]["codigo"] == 1
    assert corpo["status_atual"]["grupo"] == "preparando"
    assert corpo["status_atual"]["rotulo"] == "Aguardando coleta / postagem"
    assert len(corpo["historico"]) == 2


def test_resposta_nunca_e_cacheada_por_navegador_ou_cdn(
    cliente_sucesso: TestClient,
) -> None:
    r = cliente_sucesso.post(
        "/api/v1/rastreio", json={"email": EMAIL, "numero_pedido": "59552"}
    )
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["pragma"] == "no-cache"


def test_aceita_numero_com_e_sem_cerquilha(cliente_sucesso: TestClient) -> None:
    for digitado in ("59552", "#59552", " 59552 "):
        r = cliente_sucesso.post(
            "/api/v1/rastreio", json={"email": EMAIL, "numero_pedido": digitado}
        )
        assert r.status_code == 200, digitado


def test_nao_encontrado_devolve_404_generico() -> None:
    with _cliente(None, _ocorrencias()) as c:
        r = c.post("/api/v1/rastreio", json={"email": EMAIL, "numero_pedido": "99999"})

    assert r.status_code == 404
    corpo = r.json()
    assert corpo["resultado"] == Resultado.NAO_ENCONTRADO
    assert "status_atual" not in corpo
    assert "historico" not in corpo


def test_email_errado_responde_exatamente_como_pedido_inexistente() -> None:
    with _cliente(_pedido(), _ocorrencias()) as c:
        errado = c.post(
            "/api/v1/rastreio", json={"email": "invasor@exemplo.com", "numero_pedido": "59552"}
        )
    with _cliente(None, _ocorrencias()) as c:
        inexistente = c.post(
            "/api/v1/rastreio", json={"email": EMAIL, "numero_pedido": "99999"}
        )

    assert errado.status_code == inexistente.status_code == 404
    assert errado.json() == inexistente.json()


def test_sem_rastreio_devolve_200_com_historico_vazio() -> None:
    with _cliente(_pedido(tem_fulfillment=False), []) as c:
        r = c.post("/api/v1/rastreio", json={"email": EMAIL, "numero_pedido": "59552"})

    assert r.status_code == 200
    corpo = r.json()
    assert corpo["resultado"] == Resultado.SEM_RASTREIO
    assert corpo["status_atual"] is None
    assert corpo["historico"] == []
    assert corpo["mensagem"]


def test_vazio_fr_devolve_200_sem_historico() -> None:
    with _cliente(_pedido(tem_fulfillment=True, codigo_rastreio="FR260723D6KTG"), []) as c:
        r = c.post("/api/v1/rastreio", json={"email": EMAIL, "numero_pedido": "59552"})

    assert r.status_code == 200
    corpo = r.json()
    assert corpo["resultado"] == Resultado.VAZIO_FR
    assert corpo["status_atual"] is None
    assert corpo["mensagem"]


def test_nenhuma_resposta_expoe_o_identificador_da_frete_rapido() -> None:
    """O `id_frete` gravado no fulfillment nao e rastreavel na transportadora.

    Exibi-lo faria o cliente tentar usa-lo na Jadlog, falhar, e procurar o
    suporte -- o oposto do proposito da pagina.
    """
    casos = [
        (_pedido(tem_fulfillment=True, codigo_rastreio="FR260723D6KTG"), _ocorrencias()),
        (_pedido(tem_fulfillment=True, codigo_rastreio="FR260723D6KTG"), []),
    ]
    for pedido, ocorrencias in casos:
        with _cliente(pedido, ocorrencias) as c:
            r = c.post(
                "/api/v1/rastreio", json={"email": EMAIL, "numero_pedido": "59552"}
            )
        assert "FR260723D6KTG" not in r.text
        assert "codigo_rastreio" not in r.json()


def test_email_invalido_e_rejeitado_na_validacao_de_formato(
    cliente_sucesso: TestClient,
) -> None:
    r = cliente_sucesso.post(
        "/api/v1/rastreio", json={"email": "nao-e-email", "numero_pedido": "59552"}
    )
    assert r.status_code == 422


def test_rate_limit_devolve_429_apos_o_limite() -> None:
    """Defesa principal contra enumeracao de pedidos."""
    with _cliente(_pedido(), _ocorrencias()) as c:
        c.app.state.limitador = LimitadorJanelaDeslizante(limite=3, janela_s=60)  # type: ignore[attr-defined]
        corpo = {"email": EMAIL, "numero_pedido": "59552"}

        assert [
            c.post("/api/v1/rastreio", json=corpo).status_code for _ in range(3)
        ] == [200, 200, 200]

        bloqueada = c.post("/api/v1/rastreio", json=corpo)

    assert bloqueada.status_code == 429
    assert bloqueada.headers["retry-after"] == "60"
    assert bloqueada.headers["cache-control"] == "no-store"
    assert bloqueada.json()["mensagem"]


def test_resposta_bloqueada_nao_revela_se_o_pedido_existe() -> None:
    """O 429 precisa ser igual para pedido existente e inexistente.

    Divergir aqui daria ao atacante um oraculo que contorna o proprio limite.
    """
    corpo_existente = {"email": EMAIL, "numero_pedido": "59552"}
    corpo_inexistente = {"email": EMAIL, "numero_pedido": "99999"}

    with _cliente(_pedido(), _ocorrencias()) as c:
        c.app.state.limitador = LimitadorJanelaDeslizante(limite=0, janela_s=60)  # type: ignore[attr-defined]
        a = c.post("/api/v1/rastreio", json=corpo_existente)

    with _cliente(None, _ocorrencias()) as c:
        c.app.state.limitador = LimitadorJanelaDeslizante(limite=0, janela_s=60)  # type: ignore[attr-defined]
        b = c.post("/api/v1/rastreio", json=corpo_inexistente)

    assert a.status_code == b.status_code == 429
    assert a.json() == b.json()


def test_health() -> None:
    with _cliente(_pedido(), _ocorrencias()) as c:
        assert c.get("/health").json() == {"status": "ok"}


def test_docs_disponivel_fora_de_producao() -> None:
    with _cliente(_pedido(), _ocorrencias()) as c:
        assert c.get("/docs").status_code == 200
