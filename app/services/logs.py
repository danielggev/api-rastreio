"""Redacao de segredos antes de qualquer log.

O token da Frete Rapido trafega NA QUERY STRING. Excecoes do httpx
(`HTTPStatusError`, `ConnectError`) incluem a URL na mensagem -- entao um
`logger.exception` despretensioso grava o segredo em texto puro. Se os logs vao
para arquivo, Sentry ou agregador, o token vaza junto.

Toda URL e mensagem de erro passa por aqui antes de ser registrada.
"""

from __future__ import annotations

import logging
import re

_PADROES: tuple[re.Pattern[str], ...] = (
    # token=... na query string (Frete Rapido)
    re.compile(r"(?i)(token=)[^&\s\"']+"),
    # Segredo do webhook da Frete Rapido, que viaja no CAMINHO da URL. O uvicorn
    # registra o caminho de toda requisicao: sem isto, cada webhook recebido --
    # o caminho de SUCESSO, nao o de erro -- gravaria o segredo em texto puro.
    re.compile(r"(?i)(/webhook/frete-rapido/)[^/\s\"'?]+"),
    # Credenciais Shopify em qualquer variante do prefixo: shpat_ (access token),
    # shpss_ (secret key), shpca_, shppa_. Casar a familia inteira evita que um
    # prefixo novo passe despercebido.
    re.compile(r"(?i)(shp[a-z]{2}_)[A-Za-z0-9]+"),
    re.compile(r"(?i)(X-Shopify-Access-Token['\":\s]+)[^\s,'\"}]+"),
)

MASCARA = "***"


def redigir(texto: str | None) -> str:
    """Substitui segredos conhecidos por uma mascara."""
    if not texto:
        return ""
    saida = texto
    for padrao in _PADROES:
        saida = padrao.sub(rf"\1{MASCARA}", saida)
    return saida


def redigir_excecao(exc: BaseException) -> str:
    """Representacao segura de uma excecao, para log.

    Usar sempre isto em vez de `str(exc)` ou `logger.exception`, porque a
    mensagem original pode carregar a URL completa com o token.
    """
    return f"{type(exc).__name__}: {redigir(str(exc))}"


class RedatorDeSegredos(logging.Filter):
    """Redige QUALQUER registro que chegue ao handler, venha de onde vier.

    Existe porque redigir apenas o que nos escrevemos nao basta: o proprio httpx
    registra em INFO a URL completa de cada requisicao, token da query string
    incluido. Uma consulta comum, bem-sucedida, gravava o segredo no log.

    O filtro fica no HANDLER, nao no logger: filtro em logger so vale para
    registros criados nele, e nao para os que sobem de loggers filhos como
    `httpx`. No handler, tudo passa por aqui.

    Assim qualquer biblioteca futura que faca o mesmo ja nasce coberta.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Sem argumentos, `msg` JA e a mensagem final: da para redigir direto.
        if not record.args:
            if isinstance(record.msg, str):
                limpo = redigir(record.msg)
                if limpo != record.msg:
                    record.msg = limpo
            return True

        # Com argumentos, o template NAO pode ser tocado. Um padrao que casasse
        # `token=%s` devolveria `token=***`, destruindo o placeholder -- e a
        # formatacao estouraria com "not all arguments converted".
        #
        # Redigimos os ARGUMENTOS, um a um, preservando a estrutura do registro.
        # Zerar `args`, o caminho mais obvio, quebraria formatadores que dependem
        # dela: o `AccessFormatter` do uvicorn desempacota `record.args` em cinco
        # variaveis, justamente no registro em que o segredo viaja.
        if isinstance(record.args, tuple):
            novos = tuple(
                redigir(a) if isinstance(a, str) else a for a in record.args
            )
            if novos != record.args:
                record.args = novos
        elif isinstance(record.args, dict):
            novo_mapa = {
                k: (redigir(v) if isinstance(v, str) else v)
                for k, v in record.args.items()
            }
            if novo_mapa != record.args:
                record.args = novo_mapa

        # Ultimo recurso: o segredo atravessava a fronteira entre o template e um
        # argumento (`"GET %s?token=%s"`), e so aparece na mensagem formatada.
        # Aqui nao ha como preservar `args` -- mas o caso e raro e nao envolve os
        # formatadores especializados que motivam o cuidado acima.
        try:
            formatada = record.getMessage()
        except Exception:
            return True
        limpa = redigir(formatada)
        if limpa != formatada:
            record.msg = limpa
            record.args = ()
        return True


# Loggers que mantem handler proprio com `propagate=False`: registros criados
# neles NUNCA sobem ate o raiz, entao um filtro instalado la nao os alcanca.
#
# `uvicorn.access` e o caso critico -- e ele que registra o CAMINHO de cada
# requisicao, e e no caminho que viaja o segredo do webhook da Frete Rapido.
# Foi assim que o vazamento apareceu: no caminho de SUCESSO, com o filtro do
# raiz instalado e funcionando.
_LOGGERS_ISOLADOS = ("uvicorn", "uvicorn.access", "uvicorn.error", "gunicorn.access")


def _aplicar(alvo: logging.Logger) -> None:
    """Instala o filtro no logger e em todos os handlers dele. Idempotente."""
    if not any(isinstance(f, RedatorDeSegredos) for f in alvo.filters):
        alvo.addFilter(RedatorDeSegredos())
    for handler in alvo.handlers:
        if not any(isinstance(f, RedatorDeSegredos) for f in handler.filters):
            handler.addFilter(RedatorDeSegredos())


def instalar_redacao(logger_raiz: logging.Logger | None = None) -> None:
    """Instala o filtro no logger raiz e nos loggers de handler proprio.

    Chamada mais de uma vez de proposito (no import e na subida da aplicacao):
    o uvicorn configura o logging DELE em momentos que nao controlamos, e uma
    instalacao unica no import pode preceder a criacao dos handlers dele. Como e
    idempotente, chamar de novo nao duplica nada.
    """
    raiz = logger_raiz or logging.getLogger()
    for handler in raiz.handlers:
        if not any(isinstance(f, RedatorDeSegredos) for f in handler.filters):
            handler.addFilter(RedatorDeSegredos())

    for nome in _LOGGERS_ISOLADOS:
        _aplicar(logging.getLogger(nome))

    # Cinto e suspensorio: sem o INFO do httpx, some a principal fonte do
    # problema mesmo que o filtro falhe. As falhas de rede continuam visiveis,
    # porque WARNING e acima seguem passando.
    for nome in ("httpx", "httpcore"):
        logging.getLogger(nome).setLevel(logging.WARNING)
