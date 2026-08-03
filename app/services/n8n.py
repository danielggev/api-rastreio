"""Entrega do aviso ao n8n, que monta o texto e envia o WhatsApp.

A divisao de responsabilidade e deliberada: **a API decide, o n8n entrega**.
Aqui mora apenas o transporte -- nada de decidir se avisa, nem de escrever a
mensagem.

Duas observacoes que importam:

1. Este e o unico ponto do projeto onde dado pessoal SAI para um terceiro.
   Mandamos o minimo: telefone e primeiro nome. Nada de CPF, email ou endereco.
   O n8n grava os dados de execucao no banco dele por padrao, entao tudo que
   passa por aqui fica retido la, fora do alcance de `scripts/expurgar.py`.
2. O n8n nao e uma dependencia critica. Se ele estiver fora do ar, quem
   reenvia e a propria Frete Rapido -- ver `services/notificacao.py`.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.services.logs import redigir_excecao
from app.services.reintento import Permanente, Politica, Transitorio, com_reintento

logger = logging.getLogger(__name__)

# Orcamento curto de proposito. Quem espera do outro lado nao e uma pessoa
# olhando a tela, e sim a Frete Rapido aguardando o HTTP 200 -- e ela tem a
# propria escada de reentrega, bem mais paciente que qualquer laco nosso.
POLITICA_PADRAO = Politica(max_tentativas=2, orcamento_s=6.0, timeout_chamada_s=4.0)


class N8nErro(Exception):
    """Falha ao entregar o aviso ao n8n."""


class N8nNaoConfigurado(N8nErro):
    """Sem `N8N_WEBHOOK_URL`. Nao adianta repetir: e configuracao."""


class ClienteN8n:
    def __init__(
        self,
        *,
        cliente_http: httpx.AsyncClient | None = None,
        url: str | None = None,
        token: str | None = None,
        politica: Politica | None = None,
    ) -> None:
        s = get_settings()
        self._url = url if url is not None else s.n8n_webhook_url
        self._token = token if token is not None else s.n8n_webhook_token
        self._http = cliente_http
        self._politica = politica or POLITICA_PADRAO

    @property
    def configurado(self) -> bool:
        return bool(self._url)

    async def enviar(self, payload: dict[str, Any]) -> None:
        """Entrega o aviso. Levanta `N8nErro` em qualquer falha.

        Nao ha valor de retorno: o que o n8n faz com o payload nao e assunto
        nosso, e tratar a resposta dele como contrato acoplaria os dois lados
        sem necessidade.
        """
        if not self._url:
            raise N8nNaoConfigurado("N8N_WEBHOOK_URL nao configurada")

        async def chamar(timeout: float) -> None:
            await self._postar(payload, timeout)

        try:
            await com_reintento(chamar, self._politica, nome="n8n")
        except (Transitorio, Permanente) as exc:
            raise N8nErro(redigir_excecao(exc)) from None

    async def _postar(self, payload: dict[str, Any], timeout: float) -> None:
        cabecalhos = {"Content-Type": "application/json"}
        if self._token:
            # O no Webhook do n8n aceita autenticacao por cabecalho. Sem isto,
            # qualquer um que descubra a URL do n8n dispara mensagem para
            # qualquer telefone -- o mesmo problema que o segredo da nossa rota
            # resolve do outro lado.
            cabecalhos["X-Webhook-Token"] = self._token

        try:
            if self._http is not None:
                resposta = await self._http.post(
                    self._url, json=payload, headers=cabecalhos, timeout=timeout
                )
            else:
                async with httpx.AsyncClient(timeout=timeout) as http:
                    resposta = await http.post(
                        self._url, json=payload, headers=cabecalhos
                    )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            raise Transitorio(redigir_excecao(exc)) from None
        except httpx.HTTPError as exc:
            raise Permanente(redigir_excecao(exc)) from None

        if resposta.status_code == 429 or resposta.status_code >= 500:
            raise Transitorio(f"n8n devolveu HTTP {resposta.status_code}")
        if resposta.status_code >= 400:
            # 401/404 aqui e fluxo desativado ou token errado: repetir nao
            # resolve, e o evento fica `pendente` para alguem olhar.
            raise Permanente(f"n8n devolveu HTTP {resposta.status_code}")
