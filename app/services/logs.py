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
        try:
            mensagem = record.getMessage()
        except Exception:
            return True

        limpo = redigir(mensagem)
        if limpo != mensagem:
            record.msg = limpo
            record.args = ()
        return True


def instalar_redacao(logger_raiz: logging.Logger | None = None) -> None:
    """Instala o filtro em todos os handlers do logger raiz."""
    raiz = logger_raiz or logging.getLogger()
    for handler in raiz.handlers:
        if not any(isinstance(f, RedatorDeSegredos) for f in handler.filters):
            handler.addFilter(RedatorDeSegredos())

    # Cinto e suspensorio: sem o INFO do httpx, some a principal fonte do
    # problema mesmo que o filtro falhe. As falhas de rede continuam visiveis,
    # porque WARNING e acima seguem passando.
    for nome in ("httpx", "httpcore"):
        logging.getLogger(nome).setLevel(logging.WARNING)
