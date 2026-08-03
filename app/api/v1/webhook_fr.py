"""Recepcao do Webhook de Ocorrencias da Frete Rapido.

A Frete Rapido NAO assina o payload: nao ha HMAC, cabecalho assinado nem lista
de IPs publicada na documentacao. O segredo no caminho da URL e, por enquanto,
a unica barreira -- por isso ele e comparado em tempo constante, tem tamanho
minimo exigido no boot e e redigido em `services/logs.py` antes de qualquer
registro (o uvicorn loga o caminho de TODA requisicao).

Segredo errado responde **404**, nao 401: um 401 confirmaria que a rota existe e
que o formato do segredo esta certo, transformando a resposta num oraculo.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.schemas import WebhookOcorrenciaFR
from app.services.notificacao import ServicoNotificacao

router = APIRouter(prefix="/api/v1/webhook", tags=["webhook"])

# A Frete Rapido so olha o codigo de resposta. O corpo existe para o `curl` de
# verificacao e para os testes.
NAO_ENCONTRADO = JSONResponse(status_code=404, content={"detail": "Not Found"})


def obter_servico(request: Request) -> ServicoNotificacao | None:
    servico: ServicoNotificacao | None = getattr(
        request.app.state, "servico_notificacao", None
    )
    return servico


@router.post(
    "/frete-rapido/{segredo}",
    summary="Recebe uma ocorrencia da Frete Rapido",
    description=(
        "Endpoint de PUSH da Frete Rapido. Responde 200 em qualquer desfecho "
        "terminal e 503 quando o aviso precisa ser reenviado -- a Frete Rapido "
        "reenvia em 408, 429 e 5xx."
    ),
    include_in_schema=False,  # a URL carrega o segredo; nao entra no OpenAPI
)
async def receber_ocorrencia(
    segredo: str,
    evento: WebhookOcorrenciaFR,
    servico: Annotated[ServicoNotificacao | None, Depends(obter_servico)],
    s: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    # Sem segredo configurado a rota simplesmente nao existe. Isso mantem a
    # superficie fechada por padrao: quem nao usa o webhook nao precisa saber
    # que ele esta ai.
    if not s.webhook_fr_habilitado or servico is None:
        return NAO_ENCONTRADO

    # `compare_digest` e nao `==`: a comparacao ingenua vaza, pelo tempo de
    # resposta, quantos caracteres iniciais estao corretos.
    if not hmac.compare_digest(segredo, s.fr_webhook_segredo):
        return NAO_ENCONTRADO

    desfecho = await servico.processar(evento)

    return JSONResponse(
        status_code=desfecho.status_http,
        content={"status": desfecho.status.value, "grupo": desfecho.grupo.value},
        headers={"Cache-Control": "no-store"},
    )
