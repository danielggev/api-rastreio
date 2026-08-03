"""Recepcao do Webhook de Ocorrencias da Frete Rapido.

A Frete Rapido NAO assina o payload -- nao ha HMAC nem lista de IPs publicada.
Em compensacao, o painel de cadastro dela oferece autenticacao (Basic, Bearer e
headers avulsos), o que a documentacao publica nao menciona. Usamos DUAS
barreiras independentes:

1. **Segredo no caminho da URL.** Comparado em tempo constante, com tamanho
   minimo exigido no boot e redigido em `services/logs.py` -- o uvicorn registra
   o caminho de TODA requisicao, entao sem a redacao o segredo iria para o log
   a cada webhook recebido.
2. **Bearer token no cabecalho `Authorization`.** Mecanismo melhor que o
   primeiro: cabecalho nao aparece em log de acesso, nem em `Referer`, nem no
   historico de proxy. Opcional, e exigido quando configurado.

Qualquer barreira que falhe responde **404**, nao 401: um 401 confirmaria que a
rota existe e que o formato esta certo, transformando a resposta num oraculo
para quem estiver sondando.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.schemas import WebhookOcorrenciaFR
from app.services.notificacao import ServicoNotificacao

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhook", tags=["webhook"])

# A Frete Rapido so olha o codigo de resposta. O corpo existe para o `curl` de
# verificacao e para os testes.
NAO_ENCONTRADO = JSONResponse(status_code=404, content={"detail": "Not Found"})


def bearer_confere(cabecalho: str | None, esperado: str) -> bool:
    """Valida `Authorization: Bearer <token>` em tempo constante.

    O prefixo e comparado sem diferenciar caixa porque o padrao HTTP o define
    assim, e nao ha ganho em recusar `bearer` minusculo -- seria uma falha
    confusa de diagnosticar, sem nenhum beneficio de seguranca.
    """
    if not cabecalho:
        return False
    partes = cabecalho.split(None, 1)
    if len(partes) != 2 or partes[0].lower() != "bearer":
        return False
    return hmac.compare_digest(partes[1].strip(), esperado)


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
    request: Request,
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

    if s.fr_webhook_bearer and not bearer_confere(
        request.headers.get("authorization"), s.fr_webhook_bearer
    ):
        # Registrado porque o diagnostico e dificil sem isto: o segredo da URL
        # bateu, entao quem chamou conhece a rota -- ou o Bearer nao foi
        # configurado no Dash FR, ou foi configurado com outro valor. Nenhum
        # dado do cabecalho e registrado.
        logger.warning(
            "webhook com segredo de URL valido mas Bearer ausente ou incorreto"
        )
        return NAO_ENCONTRADO

    desfecho = await servico.processar(evento)

    return JSONResponse(
        status_code=desfecho.status_http,
        content={"status": desfecho.status.value, "grupo": desfecho.grupo.value},
        headers={"Cache-Control": "no-store"},
    )
