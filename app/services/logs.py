"""Redacao de segredos antes de qualquer log.

O token da Frete Rapido trafega NA QUERY STRING. Excecoes do httpx
(`HTTPStatusError`, `ConnectError`) incluem a URL na mensagem -- entao um
`logger.exception` despretensioso grava o segredo em texto puro. Se os logs vao
para arquivo, Sentry ou agregador, o token vaza junto.

Toda URL e mensagem de erro passa por aqui antes de ser registrada.
"""

from __future__ import annotations

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
