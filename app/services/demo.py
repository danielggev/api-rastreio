"""Modo demonstracao: falsifica Shopify E Frete Rapido, de forma coerente.

Por que as DUAS integracoes, e nao so a Frete Rapido: o fluxo consulta a Shopify
e valida o email ANTES de chamar a Frete Rapido. Falsificar apenas a segunda
faria todo pedido ficticio morrer em `nao_encontrado`, sem nunca chegar la.

Existe porque os dados reais nao cobrem o que a interface precisa tratar. A
operacao so produziu tres estados ate agora (`preparando`, `entregue`), enquanto
a pagina tem seis resultados possiveis -- incluindo `aguardando_retirada` e
`tentativa_falha`, que exigem acao do cliente e sao os mais dificeis de acertar
visualmente.

Os dois fakes derivam da MESMA tabela de cenarios, entao nao ha como ficarem
incoerentes entre si.

As datas sao relativas a hoje, para que "entrega atrasada" continue valendo
independentemente de quando a demonstracao for aberta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.schemas import Anomalia, OcorrenciaFR
from app.services.multi_cnpj import ResultadoBusca
from app.services.normalizacao import NumeroPedidoFR, normalizar_email
from app.services.ordenacao import indexar
from app.services.shopify import PedidoShopify

CNPJ_DEMO = "melhores"


@dataclass(frozen=True)
class Cenario:
    numero: str
    descricao: str
    # (codigo da ocorrencia, dias atras)
    eventos: list[tuple[int, int]]
    tem_fulfillment: bool = True
    previsao_em_dias: int | None = 3
    transportadora: str | None = "JADLOG LOGISTICA S.A"
    # Simula indisponibilidade das APIs externas.
    falha_externa: bool = False
    anomalias: list[Anomalia] = field(default_factory=list)


# Nomes oficiais do catalogo da Frete Rapido, para os rotulos baterem com o real.
NOMES = {
    0: "Contratado",
    1: "Aguardando coleta / postagem",
    2: "Em transito",
    3: "Entregue",
    5: "Entrega nao realizada",
    7: "Devolucao / Retorno",
    15: "Coletado / Postado",
    17: "Em rota para entrega",
    19: "Disponivel para retirada",
    32: "Destinatario ausente",
}

CENARIOS: dict[str, Cenario] = {
    c.numero: c
    for c in [
        Cenario(
            "900001",
            "Entregue",
            eventos=[(0, 10), (15, 8), (2, 6), (17, 2), (3, 1)],
            previsao_em_dias=-2,  # entregue depois do previsto
        ),
        Cenario("900002", "Em transito", eventos=[(0, 5), (15, 4), (2, 2)]),
        Cenario(
            "900003",
            "Saiu para entrega -- chega hoje",
            eventos=[(0, 6), (15, 5), (2, 3), (17, 0)],
            previsao_em_dias=0,
        ),
        Cenario(
            "900004",
            "Tentativa de entrega falha -- EXIGE ACAO",
            eventos=[(0, 8), (15, 7), (2, 5), (17, 2), (32, 2), (5, 1)],
            previsao_em_dias=-1,
        ),
        Cenario(
            "900005",
            "Aguardando retirada -- EXIGE ACAO",
            eventos=[(0, 9), (15, 8), (2, 5), (19, 2)],
            previsao_em_dias=-1,
        ),
        Cenario(
            "900006",
            "Em devolucao",
            eventos=[(0, 12), (15, 11), (2, 9), (32, 6), (5, 5), (7, 3)],
            previsao_em_dias=-4,
        ),
        Cenario(
            "900007",
            "Sucesso com transportadora e previsao NULAS",
            eventos=[(0, 3), (15, 2)],
            previsao_em_dias=None,
            transportadora=None,
        ),
        Cenario(
            "900008",
            "Sem fulfillment na Shopify, mas com ocorrencias na FR (caso real)",
            eventos=[(0, 2), (1, 2)],
            tem_fulfillment=False,
        ),
        Cenario(
            "900009",
            "Sem rastreio -- pedido em separacao",
            eventos=[],
            tem_fulfillment=False,
        ),
        Cenario(
            "900010",
            "vazio_fr -- despachado, mas a FR nao devolve ocorrencias",
            eventos=[],
            tem_fulfillment=True,
        ),
        Cenario(
            "900011",
            "Entrega ATRASADA -- previsao vencida e nao entregue",
            eventos=[(0, 14), (15, 13), (2, 10)],
            previsao_em_dias=-5,
        ),
        Cenario(
            "900012",
            "Erro externo -- API indisponivel",
            eventos=[],
            falha_externa=True,
        ),
        Cenario(
            "900013",
            "Pedido sem tag de CNPJ -- busca em todos os tokens",
            eventos=[(0, 4), (15, 3), (2, 1)],
            anomalias=[Anomalia.TAG_CNPJ_AUSENTE],
        ),
    ]
}


def _agora() -> datetime:
    return datetime.now().replace(microsecond=0)


def _ocorrencias(cenario: Cenario) -> list[OcorrenciaFR]:
    base = _agora()
    previsao: date | None = None
    if cenario.previsao_em_dias is not None:
        previsao = (base + timedelta(days=cenario.previsao_em_dias)).date()

    # Ordem cronologica crescente, como a API real entrega.
    itens = [
        OcorrenciaFR(
            codigo=codigo,
            nome=NOMES.get(codigo, f"Ocorrencia {codigo}"),
            data_ocorrencia=base - timedelta(days=dias_atras),
            data_atualizacao=base - timedelta(days=dias_atras),
            data_prevista_entrega=previsao,
            razao_social_transportadora=cenario.transportadora,
        )
        for codigo, dias_atras in cenario.eventos
    ]
    return indexar(itens)


class ShopifyDemo:
    """Substitui o `ClienteShopify`. Aceita apenas o email de demonstracao."""

    def __init__(self, email_demo: str) -> None:
        self._email = normalizar_email(email_demo)

    async def buscar_pedido(self, numero: NumeroPedidoFR) -> PedidoShopify | None:
        cenario = CENARIOS.get(str(numero))
        if cenario is None:
            return None

        return PedidoShopify(
            id=f"gid://shopify/Order/{numero}",
            name=str(numero),
            email_normalizado=self._email,
            criado_em=_agora() - timedelta(days=15),
            tem_fulfillment=cenario.tem_fulfillment,
            codigo_rastreio=f"FR2607{numero[-4:]}" if cenario.tem_fulfillment else None,
            tags=[] if cenario.anomalias else [CNPJ_DEMO],
        )


class FreteRapidoDemo:
    """Substitui o `BuscadorMultiCNPJ`, coerente com o `ShopifyDemo`."""

    async def buscar(
        self, numero: NumeroPedidoFR, tags: list[str] | None = None
    ) -> ResultadoBusca:
        cenario = CENARIOS.get(str(numero))
        if cenario is None:
            return ResultadoBusca()

        if cenario.falha_externa:
            # Sem dados E com falha: o servico deve responder erro, nunca
            # "sem rastreio".
            return ResultadoBusca(houve_falha=True, anomalias=list(cenario.anomalias))

        ocorrencias = _ocorrencias(cenario)
        return ResultadoBusca(
            ocorrencias=ocorrencias,
            cnpj=CNPJ_DEMO if ocorrencias else None,
            anomalias=list(cenario.anomalias),
        )


def catalogo() -> list[dict[str, str]]:
    """Lista os pedidos magicos, para quem estiver montando a interface."""
    return [
        {"numero": c.numero, "descricao": c.descricao} for c in CENARIOS.values()
    ]
