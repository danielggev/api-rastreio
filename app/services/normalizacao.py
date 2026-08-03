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


# DDDs que existem de fato. A lista completa cabe aqui e vale mais do que um
# `\d{2}`: "00", "30" e "60" nao sao DDD nenhum, e aceita-los produziria um
# numero E.164 bem formado que simplesmente nao pertence a ninguem.
_DDDS: frozenset[str] = frozenset(
    {
        "11", "12", "13", "14", "15", "16", "17", "18", "19",
        "21", "22", "24", "27", "28",
        "31", "32", "33", "34", "35", "37", "38",
        "41", "42", "43", "44", "45", "46", "47", "48", "49",
        "51", "53", "54", "55",
        "61", "62", "63", "64", "65", "66", "67", "68", "69",
        "71", "73", "74", "75", "77", "79",
        "81", "82", "83", "84", "85", "86", "87", "88", "89",
        "91", "92", "93", "94", "95", "96", "97", "98", "99",
    }
)

# Pontuacao que so formata: parenteses, hifen, ponto, barra, espacos e o "+" do
# prefixo internacional. Qualquer outra coisa ("ramal", "r.", letras) faz o
# numero ser RECUSADO, nunca limpo -- ver a nota em `normalizar_telefone_br`.
#
# O \u00a0 (espaco nao-separavel) esta na lista porque telefone copiado de pagina
# web costuma traze-lo no lugar do espaco comum. Escrito como ESCAPE de proposito:
# literal, ele fica invisivel na revisao do codigo.
_FORMATACAO = str.maketrans({c: None for c in "()-./ \t\u00a0"})


def normalizar_telefone_br(bruto: str | None) -> str | None:
    """Celular brasileiro em E.164 (`+5511999999999`), ou `None` se inutilizavel.

    Devolver `None` NAO e falha: e a resposta correta para "nao da para avisar
    esta pessoa por WhatsApp". Quem chama trata isso como `sem_contato`.

    Mesma disciplina do `NumeroPedidoFR`: **recusar em vez de consertar**. Um
    `re.sub(r"\\D", "", ...)` transformaria "11 99999-9999 ramal 22" em
    "11999999999922" ou, pior, produziria um numero valido de OUTRA pessoa. Aqui
    o custo do erro nao e uma consulta vazia -- e mandar dados de um pedido para
    um desconhecido.

    Duas recusas deliberadas:

    - **Fixo.** Nao existe WhatsApp em telefone fixo; a mensagem simplesmente
      nao chegaria. Recusar deixa a cadeia de fallback tentar a proxima fonte.
    - **Celular antigo de 8 digitos.** Desde 2016 todo celular tem 9 digitos, e
      a migracao foi prefixar um "9". Fazer isso aqui seria INVENTAR um digito:
      o numero resultante e plausivel e pode pertencer a outra pessoa. A taxa de
      `sem_contato` mostra se isso importa na pratica, e ai a decisao passa a ser
      informada.
    """
    if not bruto:
        return None

    limpo = bruto.strip().translate(_FORMATACAO)
    if not limpo:
        return None

    internacional = limpo.startswith("+")
    if internacional:
        limpo = limpo[1:]

    if not limpo.isdigit():
        return None

    # "00" e o prefixo de discagem INTERNACIONAL; um "0" sozinho e o nacional
    # (0xx da operadora). Tratar os dois como a mesma coisa -- um `lstrip("0")`
    # cru -- faz `0099999888899` virar um celular brasileiro plausivel, quando
    # na verdade e uma discagem internacional para um pais que nao existe.
    if limpo.startswith("00"):
        internacional = True
        limpo = limpo[2:]
    else:
        limpo = limpo.lstrip("0")

    if internacional:
        # Codigo de pais explicito: so seguimos se for o do Brasil.
        if not (limpo.startswith("55") and len(limpo) in (12, 13)):
            return None
        limpo = limpo[2:]
    elif len(limpo) in (12, 13) and limpo.startswith("55"):
        # Sem "+", o comprimento desfaz a ambiguidade do "55", que tambem e o
        # DDD de Santa Maria: em `5533334444` (10 digitos) o 55 e DDD, em
        # `5511999999999` (13 digitos) e codigo de pais.
        limpo = limpo[2:]

    if len(limpo) != 11:
        # 10 digitos = fixo; qualquer outro tamanho e lixo ou numero estrangeiro.
        return None

    ddd, numero = limpo[:2], limpo[2:]
    if ddd not in _DDDS:
        return None
    # Celular brasileiro sempre comeca com 9 apos o DDD.
    if not numero.startswith("9"):
        return None

    return f"+55{limpo}"


def primeiro_nome(completo: str | None) -> str | None:
    """Primeiro nome, para tratar o cliente pelo nome na mensagem.

    Guardar so o primeiro nome e minimizacao de dado: o nome completo nao
    acrescenta nada a uma mensagem de "sua encomenda chegou" e amplia o que
    trafega para fora daqui.
    """
    if not completo:
        return None
    partes = completo.strip().split()
    if not partes:
        return None
    return partes[0]


def truncar(texto: str | None, limite: int) -> str | None:
    """Corta texto de origem externa no limite configurado.

    `user_agent` e cabecalho controlado pelo cliente; `mensagem` e `descricao`
    vem de transportadoras terceiras. Sem teto viram vetor de inchaco do banco.
    """
    if texto is None:
        return None
    return texto[:limite]
