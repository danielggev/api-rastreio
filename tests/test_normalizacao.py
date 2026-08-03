"""Testes da normalizacao -- a mitigacao do risco numero 1.

Os afixos sao passados explicitamente em vez de virem da configuracao global,
para que os casos descrevam a regra e nao o estado do ambiente.
"""

from __future__ import annotations

import pytest

from app.services.normalizacao import (
    NumeroPedidoErro,
    NumeroPedidoFR,
    montar_name_shopify,
    normalizar_email,
    normalizar_telefone_br,
    primeiro_nome,
    truncar,
)

# Configuracao REAL da loja, verificada em 30/07/2026 com o pedido 59552:
# o `name` na Shopify e "59552", sem prefixo nenhum.
LOJA = {"prefixo": "", "sufixo": ""}


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("59552", "59552"),  # forma real do `name` na Shopify
        ("#59552", "59552"),  # cliente digitou o "#" por habito
        (" 59552 ", "59552"),
        ("  #59552  ", "59552"),
        ("##59552", "59552"),
        ("#1", "1"),
    ],
)
def test_normaliza_formas_validas(entrada: str, esperado: str) -> None:
    assert NumeroPedidoFR(entrada, **LOJA) == esperado


def test_cerquilha_digitada_e_tolerada_mesmo_sem_prefixo_configurado() -> None:
    """O "#" e marcador universal de pedido e independe do afixo da loja.

    Esta loja NAO usa prefixo, mas recusar quem digita "#59552" seria rejeitar
    um pedido legitimo por um caractere que o cliente pos por habito.
    """
    assert NumeroPedidoFR("#59552", prefixo="", sufixo="") == "59552"


def test_afixo_da_loja_nao_contamina_a_busca() -> None:
    """A reconstrucao do nome usa APENAS o afixo real da loja.

    Se o "#" tolerado na entrada virasse prefixo na busca, procurariamos por
    "#59552" numa loja cujos pedidos se chamam "59552".
    """
    numero = NumeroPedidoFR("#59552", prefixo="", sufixo="")
    assert montar_name_shopify(numero, prefixo="", sufixo="") == "59552"


@pytest.mark.parametrize(
    "entrada",
    ["", "   ", None, "abc", "#abc", "59552-A", "LOJA12-345-A", "59 552", "#", "##"],
)
def test_recusa_formato_inesperado(entrada: str | None) -> None:
    """Formato inesperado levanta excecao em vez de ser "consertado".

    `LOJA12-345-A` e o caso que justifica a regra: um `re.sub(r"\\D", "", ...)`
    o converteria em `12345` silenciosamente, inventando um pedido que nao existe.
    """
    with pytest.raises(NumeroPedidoErro):
        NumeroPedidoFR(entrada, **LOJA)


def test_sufixo_depende_da_configuracao_da_loja() -> None:
    """`1001-A` e erro OU valido conforme os afixos configurados.

    A Shopify permite nomes alfanumericos como `1001-A`. O comportamento correto
    acompanha a configuracao da loja; nao pode ser fixado no codigo.
    """
    # Configuracao atual: sem sufixo -> formato inesperado.
    with pytest.raises(NumeroPedidoErro):
        NumeroPedidoFR("#1001-A", prefixo="#", sufixo="")

    # Se a loja passar a usar o sufixo "-A", o mesmo valor vira valido.
    assert NumeroPedidoFR("#1001-A", prefixo="#", sufixo="-A") == "1001"


def test_comparacao_entre_formas_normalizadas() -> None:
    """TESTE OBRIGATORIO do plano.

    O `name` da Shopify e `#59552`; o input do cliente e `59552`. Comparados
    literalmente nunca casariam, e a API rejeitaria TODO pedido legitimo.
    """
    da_shopify = NumeroPedidoFR("#59552", **LOJA)
    do_cliente = NumeroPedidoFR("59552", **LOJA)
    assert da_shopify == do_cliente


def test_ida_e_volta_com_a_shopify() -> None:
    numero = NumeroPedidoFR("59552", **LOJA)
    assert montar_name_shopify(numero, **LOJA) == "59552"


def test_montar_name_com_sufixo() -> None:
    numero = NumeroPedidoFR("1001", prefixo="#", sufixo="-A")
    assert montar_name_shopify(numero, prefixo="#", sufixo="-A") == "#1001-A"


def test_numero_normalizado_nunca_carrega_o_caractere_cerquilha() -> None:
    """Anti-regressao: o valor que vai para a URL da Frete Rapido so tem digitos."""
    numero = NumeroPedidoFR("#59552", **LOJA)
    assert "#" not in numero
    assert numero.isdigit()


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("Cliente@Exemplo.com.BR", "cliente@exemplo.com.br"),
        ("  cliente@exemplo.com  ", "cliente@exemplo.com"),
        ("CLIENTE@EXEMPLO.COM", "cliente@exemplo.com"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalizar_email(entrada: str | None, esperado: str | None) -> None:
    assert normalizar_email(entrada) == esperado


def test_email_normalizado_e_estavel() -> None:
    """A mesma funcao alimenta comparacao e HMAC; formas equivalentes devem convergir."""
    formas = ["Cliente@Exemplo.com", " cliente@exemplo.com ", "CLIENTE@EXEMPLO.COM"]
    assert len({normalizar_email(f) for f in formas}) == 1


def test_truncar() -> None:
    assert truncar("a" * 600, 512) == "a" * 512
    assert truncar("curto", 512) == "curto"
    assert truncar(None, 512) is None


# --------------------------------------------------------------------------
# Telefone -- o aviso proativo por WhatsApp
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("11999998888", "+5511999998888"),          # forma nua
        ("(11) 99999-8888", "+5511999998888"),      # como a Shopify costuma devolver
        ("+55 11 99999-8888", "+5511999998888"),    # ja internacional
        ("5511999998888", "+5511999998888"),        # com DDI, sem "+"
        ("011999998888", "+5511999998888"),         # prefixo de discagem nacional
        ("11 9 9999-8888", "+5511999998888"),       # nono digito separado
        ("\u00a011999998888 ", "+5511999998888"),      # espaco nao-separavel de copiar/colar
        ("21999998888", "+5521999998888"),
    ],
)
def test_telefone_valido_vira_e164(entrada: str, esperado: str) -> None:
    assert normalizar_telefone_br(entrada) == esperado


@pytest.mark.parametrize(
    "entrada",
    [
        None,
        "",
        "   ",
        "1133334444",        # fixo: nao existe WhatsApp em telefone fixo
        "(11) 3333-4444",    # idem, formatado
        "999998888",         # sem DDD: nao da para adivinhar
        "1199999888",        # 10 digitos comecando com 9: nem fixo nem celular
        # "00" e discagem INTERNACIONAL: sem o codigo 55 depois dele, nao e
        # Brasil. Um `lstrip("0")` cru transformaria isto num celular plausivel.
        "0099999888899",
        "+1 415 555 2671",   # numero estrangeiro
        "+11999998888",      # codigo de pais 1 (EUA), nao DDD 11
        "11999998888 ramal 22",
        "nao-e-telefone",
        "1099999888 8",      # DDD 10 nao existe
        "3099999888 8",      # DDD 30 nao existe
    ],
)
def test_telefone_inutilizavel_devolve_none(entrada: str | None) -> None:
    """`None` nao e falha: e a resposta correta para "nao da para avisar"."""
    assert normalizar_telefone_br(entrada) is None


def test_celular_antigo_de_oito_digitos_e_RECUSADO() -> None:
    """Prefixar o nono digito seria INVENTAR informacao.

    O numero resultante e plausivel e pode pertencer a outra pessoa -- e o custo
    do erro aqui nao e uma consulta vazia, e mandar dados de um pedido para um
    desconhecido. A taxa de `sem_contato` mostra se isso importa na pratica.
    """
    assert normalizar_telefone_br("1199998888") is None


def test_ddd_55_nao_e_confundido_com_codigo_do_pais() -> None:
    """55 e o DDD de Santa Maria/RS. Decidir por prefixo erraria um dos dois casos."""
    # 11 digitos: 55 e DDD, e o numero e celular.
    assert normalizar_telefone_br("55999998888") == "+5555999998888"
    # 13 digitos: 55 e codigo do pais, o DDD e 11.
    assert normalizar_telefone_br("5511999998888") == "+5511999998888"


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("Daniel", "Daniel"),
        ("Daniel Silva Souza", "Daniel"),
        ("  Ana  Maria ", "Ana"),
        (None, None),
        ("", None),
        ("   ", None),
    ],
)
def test_primeiro_nome(entrada: str | None, esperado: str | None) -> None:
    """Minimizacao: o nome completo nao acrescenta nada a "sua encomenda chegou"."""
    assert primeiro_nome(entrada) == esperado
