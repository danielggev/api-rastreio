"""Aplicacao FastAPI."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.v1 import rastreio, webhook_fr
from app.config import Settings, get_settings
from app.db.session import criar_engine, criar_fabrica
from app.middleware.protecao import ProtecaoMiddleware
from app.middleware.rate_limit import LimitadorJanelaDeslizante, interpretar_limite
from app.services.auditoria import Auditoria
from app.services.cache import Cache, CacheDesligado, CacheMemoria, CachePostgres
from app.services.consulta import ServicoConsulta
from app.services.demo import FreteRapidoDemo, ShopifyDemo, catalogo
from app.services.eventos import EventosMemoria, EventosPostgres, RepositorioEventos
from app.services.frete_rapido import ClienteFreteRapido
from app.services.logs import instalar_redacao
from app.services.multi_cnpj import BuscadorMultiCNPJ
from app.services.notificacao import ServicoNotificacao
from app.services.shopify import ClienteShopify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Instalado IMEDIATAMENTE apos o basicConfig, antes de qualquer requisicao: o
# httpx registra em INFO a URL completa de cada chamada, com o token da Frete
# Rapido na query string. Sem isto, uma consulta bem-sucedida grava o segredo.
instalar_redacao()

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


def montar_notificacao(s: Settings, eventos: RepositorioEventos) -> ServicoNotificacao:
    """Servico do webhook. Em DEMO_MODE usa a Shopify falsa, como o de consulta.

    Os pedidos de demonstracao NAO tem telefone, de proposito: qualquer numero
    plausivel que escrevessemos aqui pertence a alguem de verdade. Em DEMO_MODE
    o webhook portanto sempre termina em `sem_contato` -- que e um desfecho
    legitimo. Os demais caminhos sao exercitados pelos testes, onde o contato e
    controlado.
    """
    if s.demo_mode:
        return ServicoNotificacao(
            shopify=ShopifyDemo(s.demo_email),  # type: ignore[arg-type]
            eventos=eventos,
            settings=s,
            frete_rapido=FreteRapidoDemo(),  # type: ignore[arg-type]
        )
    return ServicoNotificacao(
        shopify=ClienteShopify(),
        eventos=eventos,
        settings=s,
        # A MESMA instancia de busca do fluxo de consulta: confirmar o evento na
        # fonte e exatamente a consulta que ja sabemos fazer, com o token do CNPJ
        # certo e a politica de reintento ja calibrada.
        frete_rapido=BuscadorMultiCNPJ(ClienteFreteRapido(), s.tokens_frete_rapido),
    )


def _registrar_gatilhos(s: Settings) -> None:
    """Deixa a configuracao EFETIVA visivel em `docker compose logs`.

    Sem isto, "quais status estao ativos agora" so se responde inspecionando o
    `.env` do servidor -- e um aviso que deixou de sair nao deixa rastro nenhum
    para denunciar a configuracao errada.
    """
    if not s.webhook_fr_habilitado:
        logger.info("webhook da Frete Rapido DESLIGADO (FR_WEBHOOK_SEGREDO vazio)")
        return

    logger.info(
        "webhook da Frete Rapido ativo | envio=%s | confirma na fonte=%s | "
        "grupos=%s | extra=%s | ignorados=%s | teto=%d aviso(s)/%dh | CNPJs=%s",
        "LIGADO" if s.notificacao_ativa else "DESLIGADO (modo observacao)",
        "sim" if s.notificacao_verificar_na_fonte else "NAO",
        sorted(g.value for g in s.grupos_notificaveis) or "(nenhum)",
        sorted(s.codigos_extra) or "(nenhum)",
        sorted(s.codigos_ignorados) or "(nenhum)",
        s.notificacao_max_por_pedido,
        s.notificacao_janela_horas,
        sorted(c for c in s.segredos_webhook.values() if c) or "(segredo unico)",
    )


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()

    # Reinstalado aqui, e nao apenas no import: quando rodamos sob uvicorn, os
    # handlers DELE sao criados fora do nosso controle e podem nascer depois do
    # import deste modulo. Este ponto executa com o logging ja inteiro montado.
    # E o `uvicorn.access` e justamente quem registra o caminho da requisicao,
    # onde viaja o segredo do webhook.
    instalar_redacao()

    # O banco e opcional: sem ele a API responde normalmente, apenas sem
    # auditoria e com cache em memoria. Preferimos servir o cliente a exigir
    # infraestrutura completa para funcionar.
    fabrica = None
    cache: Cache = CacheMemoria()
    eventos: RepositorioEventos = EventosMemoria()
    if s.database_url:
        try:
            engine = criar_engine(s.database_url)
            fabrica = criar_fabrica(engine)
            cache = CachePostgres(fabrica)
            eventos = EventosPostgres(fabrica)
            app.state.engine = engine
        except Exception as exc:
            logger.error("banco indisponivel, seguindo sem auditoria: %s", exc)

    app.state.auditoria = Auditoria(fabrica)
    # Exposta para o readiness consultar as tabelas.
    app.state.fabrica_sessao = fabrica

    limite, janela = interpretar_limite(s.rate_limit)
    app.state.limitador = LimitadorJanelaDeslizante(limite, janela)
    limite_wh, janela_wh = interpretar_limite(s.rate_limit_webhook)
    app.state.limitador_webhook = LimitadorJanelaDeslizante(limite_wh, janela_wh)

    # Nao sobrescreve um servico ja injetado: e assim que os testes (e o modo
    # demonstracao) substituem as integracoes reais.
    if getattr(app.state, "servico_consulta", None) is None:
        app.state.servico_consulta = montar_servico(s, cache)

    if getattr(app.state, "servico_notificacao", None) is None:
        app.state.servico_notificacao = montar_notificacao(s, eventos)

    logger.info(
        "API iniciada (env=%s, Shopify=%s, fuso FR=%s, CNPJs=%s, limite=%s, banco=%s)",
        s.env,
        s.shopify_api_version,
        s.frete_rapido_timezone,
        list(s.tokens_frete_rapido.keys()) or "(nenhum)",
        s.rate_limit,
        "sim" if fabrica else "nao",
    )
    _registrar_gatilhos(s)
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

    # Rate limit e limite de corpo ANTES da validacao do Pydantic. Dentro da
    # rota, requisicoes malformadas (422) escapavam do limite e corpos enormes
    # eram lidos e validados antes de qualquer controle.
    app.add_middleware(
        ProtecaoMiddleware,
        proxies_confiaveis=s.lista_proxies,
    )

    app.include_router(rastreio.router)
    # A rota so responde com FR_WEBHOOK_SEGREDO configurado; sem ele devolve 404
    # como qualquer caminho inexistente.
    app.include_router(webhook_fr.router)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Liveness: o processo esta de pe e respondendo."""
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def readiness(resposta: Response) -> dict[str, object]:
        """Readiness: as dependencias estao utilizaveis?

        `/health` responder ok nao significa que o servico esta inteiro -- a API
        continua atendendo com o banco fora, so que sem auditoria e sem cache.
        Sem esta rota, um deploy que esquecesse `alembic upgrade head` passaria
        despercebido pelo monitoramento por tempo indeterminado.
        """
        detalhes: dict[str, object] = {"api": "ok"}
        pronto = True

        fabrica = getattr(app.state, "fabrica_sessao", None)
        if fabrica is None:
            detalhes["banco"] = "nao configurado"
        else:
            try:
                async with fabrica() as sessao_teste:
                    # Consulta a tabela real: conexao viva nao garante que as
                    # migrations rodaram.
                    await sessao_teste.execute(text("SELECT 1 FROM consulta_log LIMIT 1"))
                detalhes["banco"] = "ok"
            except Exception as exc:
                detalhes["banco"] = f"indisponivel: {type(exc).__name__}"
                pronto = False

        detalhes["pronto"] = pronto
        if not pronto:
            resposta.status_code = 503
        return detalhes

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
