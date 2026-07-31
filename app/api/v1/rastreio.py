"""Rota de consulta de rastreio."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.middleware.ip_cliente import ip_do_cliente
from app.middleware.rate_limit import LimitadorJanelaDeslizante
from app.schemas import ConsultaRequest
from app.services.auditoria import Auditoria
from app.services.consulta import ServicoConsulta

router = APIRouter(prefix="/api/v1", tags=["rastreio"])

CABECALHOS_SEM_CACHE = {
    # O corpo carrega dados de um pedido especifico: nao pode ser retido por
    # navegador, proxy ou CDN -- especialmente em aparelho compartilhado.
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}

MSG_LIMITE = (
    "Muitas consultas em pouco tempo. Aguarde um minuto e tente novamente."
)


def obter_servico(request: Request) -> ServicoConsulta:
    servico: ServicoConsulta = request.app.state.servico_consulta
    return servico


def obter_limitador(request: Request) -> LimitadorJanelaDeslizante:
    limitador: LimitadorJanelaDeslizante = request.app.state.limitador
    return limitador


def obter_auditoria(request: Request) -> Auditoria:
    auditoria: Auditoria = request.app.state.auditoria
    return auditoria


@router.post(
    "/rastreio",
    summary="Consulta o rastreio de um pedido",
    description=(
        "Valida email + numero do pedido na Shopify e devolve as ocorrencias de "
        "entrega da Frete Rapido. O campo `resultado` discrimina a forma da "
        "resposta."
    ),
)
async def consultar_rastreio(
    dados: ConsultaRequest,
    request: Request,
    servico: Annotated[ServicoConsulta, Depends(obter_servico)],
    limitador: Annotated[LimitadorJanelaDeslizante, Depends(obter_limitador)],
    auditoria: Annotated[Auditoria, Depends(obter_auditoria)],
    s: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    # 1. Rate limit, com o IP REAL do cliente (ver ip_cliente.py).
    ip = ip_do_cliente(request, s.lista_proxies)
    if not limitador.permitir(ip):
        return JSONResponse(
            status_code=429,
            content={"resultado": "limite_excedido", "mensagem": MSG_LIMITE},
            headers={**CABECALHOS_SEM_CACHE, "Retry-After": "60"},
        )

    inicio = time.monotonic()
    consulta = await servico.consultar(dados.email, dados.numero_pedido)
    latencia_ms = int((time.monotonic() - inicio) * 1000)

    await auditoria.registrar(
        consulta,
        email=dados.email,
        numero_pedido=dados.numero_pedido,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
        latencia_ms=latencia_ms,
    )

    return JSONResponse(
        status_code=consulta.status_http,
        content=consulta.resposta.model_dump(mode="json"),
        headers=CABECALHOS_SEM_CACHE,
    )
