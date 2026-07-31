"""Normalizacao de numero de pedido e email.

Este modulo existe por causa do risco numero 1 do projeto: o `name` da Shopify
inclui "#" (`#59552`), enquanto a Frete Rapido espera `59552`. Um "#" que escape
faz TODA consulta voltar vazia, sem erro nenhum -- falha silenciosa que se
disfarca de "pedido sem rastreio".

A defesa e por construcao, nao por disciplina: `NumeroPedidoFR` so consegue
existir num formato valido, e o cliente da Frete Rapido aceita exclusivamente
esse tipo.
"""

from __future__ import annotations

import re

from app.config import get_settings

_SO_DIGITOS = re.compile(r"\d+")


class NumeroPedidoErro(ValueError):
    """Numero de pedido em formato inesperado."""


class NumeroPedidoFR(str):
    """Numero de pedido no formato que a Frete Rapido entende: apenas digitos.

    Nunca usar `re.sub(r"\\D", "", ...)` para chegar aqui: isso converteria
    `LOJA12-345-A` em `12345` silenciosamente, inventando um numero que nao
    existe. Removemos apenas afixos conhecidos e recusamos o resto.

    Ha DOIS conceitos distintos, que uma versao anterior confundia:

    - AFIXO DA LOJA (`SHOPIFY_ORDER_PREFIX`/`SUFFIX`): faz parte do `name` real
      do pedido na Shopify. Usado tambem para reconstruir o nome na busca.
      Nesta loja o `name` e `59552`, sem prefixo nenhum.
    - TOLERANCIA DE DIGITACAO: o "#" que um cliente pode digitar por habito,
      mesmo que nao faca parte do nome. Sempre aceito, nunca reconstruido.

    Confundir os dois faria a busca procurar por `#59552` numa loja cujos
    pedidos se chamam `59552`.
    """

    __slots__ = ()

    def __new__(
        cls,
        bruto: str | None,
        *,
        prefixo: str | None = None,
        sufixo: str | None = None,
    ) -> NumeroPedidoFR:
        if prefixo is None or sufixo is None:
            s = get_settings()
            prefixo = s.shopify_order_prefix if prefixo is None else prefixo
            sufixo = s.shopify_order_suffix if sufixo is None else sufixo

        limpo = (bruto or "").strip()

        # Remocao condicional: o cliente pode digitar com ou sem o afixo.
        if prefixo and limpo.startswith(prefixo):
            limpo = limpo[len(prefixo) :]
        if sufixo and limpo.endswith(sufixo):
            limpo = limpo[: -len(sufixo)]

        limpo = limpo.strip()

        # Tolerancia de digitacao, independente do afixo configurado: o "#" e o
        # marcador universal de pedido e o cliente pode digita-lo por habito.
        # Recusar por causa disso seria rejeitar um pedido legitimo.
        limpo = limpo.lstrip("#").strip()

        if not _SO_DIGITOS.fullmatch(limpo):
            raise NumeroPedidoErro(
                f"numero de pedido em formato inesperado: {bruto!r}"
            )

        return super().__new__(cls, limpo)


def montar_name_shopify(
    numero: NumeroPedidoFR,
    *,
    prefixo: str | None = None,
    sufixo: str | None = None,
) -> str:
    """Reconstroi o `name` completo (`59552` -> `#59552`) para a busca na Shopify.

    Usa os MESMOS valores de configuracao da remocao, para que ida e volta sejam
    simetricas.
    """
    if prefixo is None or sufixo is None:
        s = get_settings()
        prefixo = s.shopify_order_prefix if prefixo is None else prefixo
        sufixo = s.shopify_order_suffix if sufixo is None else sufixo
    return f"{prefixo}{numero}{sufixo}"


def normalizar_email(bruto: str | None) -> str | None:
    """Forma canonica de um email.

    Funcao UNICA, usada tanto na comparacao quanto no calculo do HMAC. Se as duas
    divergissem, o mesmo email geraria hashes diferentes e a correlacao dos logs
    quebraria sem nenhum sintoma visivel.

    `casefold()` em vez de `lower()`: e a operacao correta para comparacao sem
    diferenciar maiusculas em Unicode.
    """
    if not bruto:
        return None
    limpo = bruto.strip().casefold()
    return limpo or None


def truncar(texto: str | None, limite: int) -> str | None:
    """Corta texto de origem externa no limite configurado.

    `user_agent` e cabecalho controlado pelo cliente; `mensagem` e `descricao`
    vem de transportadoras terceiras. Sem teto viram vetor de inchaco do banco.
    """
    if texto is None:
        return None
    return texto[:limite]
