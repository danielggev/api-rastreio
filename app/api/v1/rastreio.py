"""Rota de consulta de rastreio."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.middleware.ip_cliente import ip_do_cliente
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


def obter_servico(request: Request) -> ServicoConsulta:
    servico: ServicoConsulta = request.app.state.servico_consulta
    return servico


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
    auditoria: Annotated[Auditoria, Depends(obter_auditoria)],
    s: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    # O rate limit e o limite de corpo rodam no MIDDLEWARE, antes da validacao
    # do Pydantic. Aqui dentro, requisicoes malformadas ja teriam escapado do
    # limite -- o FastAPI rejeita com 422 antes de chegar nesta funcao.
    ip = ip_do_cliente(request, s.lista_proxies)

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
