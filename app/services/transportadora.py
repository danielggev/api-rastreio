"""Nome de exibicao da transportadora.

A Frete Rapido devolve a RAZAO SOCIAL -- "EMPRESA BRASILEIRA DE CORREIOS E
TELEGRAFOS", "JADLOG LOGISTICA S.A". O cliente nao reconhece esses nomes; ele
conhece "Correios" e "Jadlog".

Duas camadas:

1. Transportadoras conhecidas, com o nome comercial correto (inclusive
   acentuacao e caixa, como "FedEx" e "Mandae").
2. Para as desconhecidas, uma limpeza generica: remove sufixos societarios e
   aplica caixa de titulo. Sem isso, uma transportadora nova apareceria em
   CAIXA ALTA no meio da pagina, gritando com o cliente.
"""

from __future__ import annotations

import re

# Verificado por SUBSTRING na razao social em caixa alta. A ordem importa:
# a primeira que casar vence, entao termos mais especificos vem antes.
_CONHECIDAS: tuple[tuple[str, str], ...] = (
    ("CORREIOS", "Correios"),
    ("JADLOG", "Jadlog"),
    ("TOTAL EXPRESS", "Total Express"),
    ("AZUL", "Azul Cargo"),
    ("BRASPRESS", "Braspress"),
    ("RODONAVES", "Rodonaves"),
    ("DIRECTLOG", "Directlog"),
    ("SEQUOIA", "Sequoia"),
    ("GOLLOG", "Gollog"),
    ("LATAM", "Latam Cargo"),
    ("MANDAE", "Mandae"),
    ("LOGGI", "Loggi"),
    ("JAMEF", "Jamef"),
    ("PATRUS", "Patrus"),
    ("TNT", "TNT"),
    ("DHL", "DHL"),
    ("FEDEX", "FedEx"),
    ("UPS", "UPS"),
)

# Sufixos societarios: nao dizem nada ao cliente.
_SUFIXOS = re.compile(
    r"\b(S[/.]?A|LTDA|EIRELI|ME|EPP|MEI|EPP|CIA|COMPANHIA|"
    r"TRANSPORTES?|LOGISTICA|ENCOMENDAS|EXPRESSO?)\b\.?",
    re.IGNORECASE,
)

# Palavras que ficam em minusculo no meio de um nome proprio.
_MINUSCULAS = {"de", "da", "do", "das", "dos", "e"}


def _titulo(texto: str) -> str:
    palavras = texto.split()
    saida: list[str] = []
    for i, palavra in enumerate(palavras):
        minuscula = palavra.lower()
        # A primeira palavra sempre capitalizada, mesmo sendo conectivo.
        if i > 0 and minuscula in _MINUSCULAS:
            saida.append(minuscula)
        else:
            saida.append(minuscula.capitalize())
    return " ".join(saida)


def nome_amigavel(razao_social: str | None) -> str | None:
    """Nome comercial a partir da razao social. `None` continua `None`."""
    if not razao_social:
        return None

    bruto = razao_social.strip()
    if not bruto:
        return None

    alvo = bruto.upper()
    for termo, nome in _CONHECIDAS:
        if termo in alvo:
            return nome

    # Desconhecida: limpa o que e juridiquês e normaliza a caixa.
    limpo = _SUFIXOS.sub(" ", bruto)
    limpo = re.sub(r"[\s\-.,]+", " ", limpo).strip()

    # Se a limpeza levou tudo (razao social feita so de sufixos), preserva o
    # original em vez de devolver vazio.
    if not limpo:
        return _titulo(bruto)

    return _titulo(limpo)
