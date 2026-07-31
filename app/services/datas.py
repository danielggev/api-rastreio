"""Fuso horario das datas da Frete Rapido.

As datas chegam SEM fuso (`"2026-07-23 15:37:12"`). As amostras provam que sao
naive; NAO provam qual fuso representam. Tratar como fato seria arriscar exibir
horarios deslocados -- a diferenca para UTC sao 3 horas, suficiente para mostrar
a data errada perto da meia-noite.

O que fazemos aqui e ATRIBUICAO de fuso de origem, nao conversao: so depois de
declarar de onde o horario vem faz sentido converte-lo para exibicao.

CONFIRMADO em 30/07/2026 como horario de Brasilia, cruzando as duas fontes no
pedido 59552:

    Shopify  createdAt      = 2026-07-23 18:19:36 UTC  (15:19:36 em Sao Paulo)
    Frete Rapido Contratado = 2026-07-23 15:37:12      (sem fuso)

Se as datas da Frete Rapido fossem UTC, a contratacao do frete teria ocorrido
quase 3 horas ANTES da compra existir. Lidas como horario de Brasilia, o frete
e contratado 18 minutos apos o pedido -- o que faz sentido.

Segue configuravel: a inferencia vale para esta operacao, nao e garantia
contratual da Frete Rapido.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings


def fuso_origem(nome: str | None = None) -> ZoneInfo:
    return ZoneInfo(nome or get_settings().frete_rapido_timezone)


def hoje_local(tz: ZoneInfo | None = None) -> date:
    """Data corrente no fuso de exibicao.

    Usar `date.today()` do servidor daria a data errada se a maquina rodar em
    UTC: entre 21h e meia-noite em Sao Paulo, ja e o dia seguinte em UTC.
    """
    return datetime.now(tz or fuso_origem()).date()


def entrega_atrasada(
    previsao: date | None, entregue: bool, tz: ZoneInfo | None = None
) -> bool:
    """A previsao venceu e a encomenda nao chegou.

    Sem isto, a pagina exibiria "Previsao de entrega: 29/07" no dia 30 -- que o
    cliente le como defeito do sistema, e nao como frete atrasado.
    """
    if previsao is None or entregue:
        return False
    return previsao < hoje_local(tz)


def atribuir_fuso(bruta: datetime | None, tz: ZoneInfo | None = None) -> datetime | None:
    """Declara de qual fuso a data naive veio. NAO converte instante.

    Se a data ja vier com fuso (a API pode mudar), respeitamos o que veio em vez
    de sobrescrever -- reatribuir corromperia o instante.
    """
    if bruta is None:
        return None
    if bruta.tzinfo is not None:
        return bruta
    return bruta.replace(tzinfo=tz or fuso_origem())
