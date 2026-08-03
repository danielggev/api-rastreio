"""Protecao aplicada ANTES da leitura e validacao do corpo.

O rate limit vivia dentro da funcao da rota, o que deixava duas brechas:

1. Requisicoes malformadas (HTTP 422) nao consumiam o limite -- da para
   martelar a API indefinidamente desde que o JSON seja invalido.
2. Corpos gigantes eram lidos e validados pelo Pydantic antes de qualquer
   limite ser consultado, gastando CPU e memoria de graca.

Como middleware, o limite passa a valer para TUDO que chega na rota, inclusive
o que o FastAPI rejeitaria depois.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.middleware.ip_cliente import ip_do_cliente
from app.middleware.rate_limit import LimitadorJanelaDeslizante

logger = logging.getLogger(__name__)

# O corpo legitimo tem dois campos curtos -- e-mail e numero do pedido. 8 KB ja
# e generoso; alem disso e abuso ou erro de integracao.
LIMITE_CORPO_BYTES = 8 * 1024

# O webhook da Frete Rapido e outra conversa: o payload traz notas fiscais,
# metadados do ERP e comprovantes. Mesmo descartando quase tudo no parsing, o
# corpo CHEGA inteiro -- medir pelo formulario do cliente rejeitaria eventos
# legitimos com 413, e a Frete Rapido nem reenvia nesse codigo.
LIMITE_CORPO_WEBHOOK_BYTES = 128 * 1024

# A Frete Rapido chega de poucos IPs e em rajada. Submete-la ao limite calibrado
# para humanos (10/minuto por IP) faria um lote de ocorrencias levar 429 --
# reenviado, mas com horas de atraso, num aviso que existe para ser urgente.
PREFIXO_WEBHOOK = "/api/v1/webhook/"

CABECALHOS = {"Cache-Control": "no-store", "Pragma": "no-cache"}

MSG_LIMITE = "Muitas consultas em pouco tempo. Aguarde um minuto e tente novamente."
MSG_GRANDE = "Requisicao invalida."


class ProtecaoMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: object,
        *,
        proxies_confiaveis: list[str],
        prefixo: str = "/api/",
        prefixo_webhook: str = PREFIXO_WEBHOOK,
        limite_corpo: int = LIMITE_CORPO_BYTES,
        limite_corpo_webhook: int = LIMITE_CORPO_WEBHOOK_BYTES,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._proxies = proxies_confiaveis
        self._prefixo = prefixo
        self._prefixo_webhook = prefixo_webhook
        self._limite_corpo = limite_corpo
        self._limite_corpo_webhook = limite_corpo_webhook
        self._requisicoes = 0

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # `/health` fica de fora: monitoramento nao pode ser bloqueado por
        # limite, e nao aceita corpo.
        if not request.url.path.startswith(self._prefixo):
            return await call_next(request)

        # Lido do `app.state` a cada requisicao, e nao guardado no __init__:
        # assim trocar o limitador (nos testes, ou em runtime) tem efeito
        # imediato, sem recriar o middleware.
        #
        # O webhook tem limitador e teto de corpo PROPRIOS: e trafego de
        # servidor, nao de navegador, e as duas grandezas sao de outra ordem.
        # Continua limitado -- so que num patamar que a Frete Rapido nao atinge
        # em operacao normal, mas que ainda contem uma rajada anomala.
        limitador: LimitadorJanelaDeslizante
        if request.url.path.startswith(self._prefixo_webhook):
            limitador = request.app.state.limitador_webhook
            limite_corpo = self._limite_corpo_webhook
        else:
            limitador = request.app.state.limitador
            limite_corpo = self._limite_corpo

        ip = ip_do_cliente(request, self._proxies)

        # Corpo grande e recusado ANTES de ser lido: `Content-Length` chega no
        # cabecalho, entao nem precisamos consumir o stream.
        tamanho = request.headers.get("content-length")
        if tamanho and tamanho.isdigit() and int(tamanho) > limite_corpo:
            logger.warning("corpo grande recusado de %s: %s bytes", ip, tamanho)
            return JSONResponse(
                status_code=413,
                content={"resultado": "requisicao_invalida", "mensagem": MSG_GRANDE},
                headers=CABECALHOS,
            )

        if not limitador.permitir(ip):
            # Registrado para que tentativas de enumeracao aparecam nos logs --
            # antes, uma rajada bloqueada nao deixava rastro nenhum.
            logger.warning("rate limit atingido por %s em %s", ip, request.url.path)
            request.state.limite_excedido = True
            return JSONResponse(
                status_code=429,
                content={"resultado": "limite_excedido", "mensagem": MSG_LIMITE},
                headers={**CABECALHOS, "Retry-After": "60"},
            )

        # Limpeza periodica: sem isto o mapa de IPs cresce enquanto o processo
        # viver. A cada 500 requisicoes o custo e irrelevante.
        self._requisicoes += 1
        if self._requisicoes % 500 == 0:
            removidas = limitador.limpar()
            if removidas:
                logger.debug("rate limit: %d chaves expiradas removidas", removidas)

        return await call_next(request)
