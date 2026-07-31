"""Aplicacao FastAPI."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.v1 import rastreio
from app.config import Settings, get_settings
from app.db.session import criar_engine, criar_fabrica
from app.middleware.rate_limit import LimitadorJanelaDeslizante, interpretar_limite
from app.services.auditoria import Auditoria
from app.services.cache import Cache, CacheDesligado, CacheMemoria, CachePostgres
from app.services.consulta import ServicoConsulta
from app.services.demo import FreteRapidoDemo, ShopifyDemo, catalogo
from app.services.frete_rapido import ClienteFreteRapido
from app.services.multi_cnpj import BuscadorMultiCNPJ
from app.services.shopify import ClienteShopify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def montar_servico(s: Settings, cache: Cache) -> ServicoConsulta:
    if s.demo_mode:
        # As DUAS integracoes sao falsificadas. Falsificar so a Frete Rapido nao
        # funcionaria: a Shopify e consultada antes e rejeitaria os pedidos
        # ficticios como `nao_encontrado`.
        logger.warning(
            "DEMO_MODE ATIVO -- dados simulados. Pedidos disponiveis: %s",
            ", ".join(c["numero"] for c in catalogo()),
        )
        return ServicoConsulta(
            shopify=ShopifyDemo(s.demo_email),  # type: ignore[arg-type]
            frete_rapido=FreteRapidoDemo(),  # type: ignore[arg-type]
            cache=CacheDesligado(),  # cache mascararia a troca de cenario
        )

    return ServicoConsulta(
        shopify=ClienteShopify(),
        frete_rapido=BuscadorMultiCNPJ(ClienteFreteRapido(), s.tokens_frete_rapido),
        cache=cache,
    )


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()

    # O banco e opcional: sem ele a API responde normalmente, apenas sem
    # auditoria e com cache em memoria. Preferimos servir o cliente a exigir
    # infraestrutura completa para funcionar.
    fabrica = None
    cache: Cache = CacheMemoria()
    if s.database_url:
        try:
            engine = criar_engine(s.database_url)
            fabrica = criar_fabrica(engine)
            cache = CachePostgres(fabrica)
            app.state.engine = engine
        except Exception as exc:
            logger.error("banco indisponivel, seguindo sem auditoria: %s", exc)

    app.state.auditoria = Auditoria(fabrica)

    limite, janela = interpretar_limite(s.rate_limit)
    app.state.limitador = LimitadorJanelaDeslizante(limite, janela)

    # Nao sobrescreve um servico ja injetado: e assim que os testes (e o modo
    # demonstracao) substituem as integracoes reais.
    if getattr(app.state, "servico_consulta", None) is None:
        app.state.servico_consulta = montar_servico(s, cache)

    logger.info(
        "API iniciada (env=%s, Shopify=%s, fuso FR=%s, CNPJs=%s, limite=%s, banco=%s)",
        s.env,
        s.shopify_api_version,
        s.frete_rapido_timezone,
        list(s.tokens_frete_rapido.keys()) or "(nenhum)",
        s.rate_limit,
        "sim" if fabrica else "nao",
    )
    yield

    motor: AsyncEngine | None = getattr(app.state, "engine", None)
    if motor is not None:
        await motor.dispose()


def criar_app() -> FastAPI:
    # Validar aqui garante que configuracao invalida derruba o boot -- inclusive
    # a trava que impede DEMO_MODE em producao.
    s = get_settings()

    app = FastAPI(
        title="API de Consulta de Rastreio",
        version="0.1.0",
        lifespan=ciclo_de_vida,
        # `/docs` nao expoe segredo, mas facilita mapear e sondar a API.
        # Custo zero para fechar em producao.
        docs_url=None if s.producao else "/docs",
        redoc_url=None if s.producao else "/redoc",
        openapi_url=None if s.producao else "/openapi.json",
    )

    # CORS e higiene de integracao, NAO barreira de seguranca: restringe apenas
    # navegadores. Qualquer script ou servidor o ignora. A protecao real contra
    # enumeracao e o rate limiting mais a validacao de email.
    #
    # Em desenvolvimento (e em DEMO_MODE) liberamos qualquer origem, para que uma
    # pagina de prototipo aberta de `file://` (origem "null") ou servida em
    # qualquer porta local funcione sem ficar caçando configuracao de CORS.
    #
    # Nao enfraquece producao: `ENV=production` cai no ramo restrito, e o
    # DEMO_MODE nem sobe la -- a validacao da configuracao impede.
    if s.demo_mode or s.env == "development":
        logger.warning(
            "CORS liberado para qualquer origem (env=%s, demo=%s). "
            "Em producao apenas %s serao aceitos.",
            s.env,
            s.demo_mode,
            s.lista_cors or "(nenhum configurado)",
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["POST", "GET", "OPTIONS"],
            allow_headers=["*"],
        )
    elif s.lista_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=s.lista_cors,
            allow_methods=["POST"],
            allow_headers=["Content-Type"],
        )

    app.include_router(rastreio.router)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    if s.demo_mode:

        @app.get("/api/v1/demo", tags=["demo"])
        async def cenarios_demo() -> dict[str, object]:
            """Pedidos disponiveis no modo demonstracao.

            Existe para quem esta montando a interface descobrir os cenarios sem
            precisar ler o codigo. So aparece com DEMO_MODE ligado.
            """
            return {
                "email": s.demo_email,
                "aviso": "Dados SIMULADOS. Qualquer outro numero devolve nao_encontrado.",
                "pedidos": catalogo(),
            }

    return app


app = criar_app()
