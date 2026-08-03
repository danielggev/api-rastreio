"""Cobertura de telefone nos pedidos despachados -- decide o desenho da Fase 2.

    .venv\\Scripts\\python.exe scripts/checar_telefone.py
    .venv\\Scripts\\python.exe scripts/checar_telefone.py --dias 60 --max 500

SOMENTE LEITURA. Nada e criado, alterado ou cancelado; rodar contra producao e
seguro.

Responde as duas perguntas que travam a implementacao do aviso proativo:

1. **O app tem acesso a `phone`?** Desde 2023 a Shopify trata telefone, nome e
   endereco como *protected customer data*, com aprovacao separada do escopo
   `read_orders`. Se faltar, a consulta volta ACCESS_DENIED e o passo "resolver
   o contato na Shopify" precisa mudar de desenho.

2. **Quantos pedidos tem telefone USAVEL?** Nao basta o campo estar preenchido:
   fixo e celular antigo de 8 digitos nao recebem WhatsApp. Por isso a medicao
   usa `normalizar_telefone_br`, exatamente a mesma funcao do caminho de
   producao -- um levantamento por SQL ou pelo GraphiQL responderia "preenchido",
   que e outra pergunta.

Cobertura baixa significa que vale implementar a segunda fonte de telefone (o
`GET quote/{id_frete}` da Frete Rapido, que devolve `destinatario.telefone`).

NENHUM telefone e impresso: a saida e agregada, e os exemplos de falha mostram
so a forma do numero, nunca os digitos.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.logs import redigir_excecao
from app.services.normalizacao import normalizar_telefone_br
from app.services.shopify import ClienteShopify, ShopifyAcessoNegado, ShopifyErro

OK = "[ok]"
FALHA = "[falha]"
ALERTA = "[!]"

# Pedidos por pagina. A Shopify limita a 250, e o custo em pontos da consulta
# cresce com o tamanho da pagina -- 100 e o meio-termo confortavel.
POR_PAGINA = 100

# A consulta e a MESMA do caminho de producao (`shopify.CONSULTA`): tira o
# telefone e o nome do ENDERECO DE ENTREGA, nao do cadastro do cliente.
#
# `customer { phone firstName }` exigiria o escopo `read_customers`, que este
# app nao tem -- e a Shopify nega a CONSULTA INTEIRA, nao so o campo.
AMOSTRA = """
query AmostraDeContato($primeiros: Int!, $cursor: String, $busca: String!) {
  orders(first: $primeiros, after: $cursor, query: $busca,
         sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      name
      createdAt
      displayFulfillmentStatus
      phone
      shippingAddress { phone firstName }
    }}
  }
}
"""


def classificar_falha(bruto: str | None) -> str:
    """Por que este numero nao serve. Alimenta a decisao, nao so a contagem."""
    if not bruto or not bruto.strip():
        return "campo vazio"

    digitos = "".join(c for c in bruto if c.isdigit())
    if digitos.startswith("55") and len(digitos) in (12, 13):
        digitos = digitos[2:]
    digitos = digitos.lstrip("0")

    if not digitos:
        return "sem digitos"
    if len(digitos) == 10 and digitos[2] in "2345":
        return "telefone FIXO (nao recebe WhatsApp)"
    if len(digitos) == 10:
        return "10 digitos, celular ANTIGO de 8 (faltando o nono)"
    if len(digitos) < 10:
        return f"curto demais ({len(digitos)} digitos, sem DDD?)"
    if len(digitos) > 11:
        return f"longo demais ({len(digitos)} digitos, estrangeiro?)"
    return "DDD inexistente ou nao comeca com 9"


async def coletar(dias: int, maximo: int) -> list[dict[str, Any]]:
    cliente = ClienteShopify()
    desde = (datetime.now(UTC) - timedelta(days=dias)).date().isoformat()
    # A populacao que importa e a dos pedidos DESPACHADOS: sao os unicos que
    # geram ocorrencia na Frete Rapido e, portanto, webhook.
    busca = f"fulfillment_status:shipped AND created_at:>={desde}"

    print(f"  Loja     : {cliente.url}")
    print(f"  Busca    : {busca!r}")
    print(f"  Teto     : {maximo} pedidos\n")

    nos: list[dict[str, Any]] = []
    cursor: str | None = None

    while len(nos) < maximo:
        corpo = await cliente.consultar_bruto(
            AMOSTRA,
            {
                "primeiros": min(POR_PAGINA, maximo - len(nos)),
                "cursor": cursor,
                "busca": busca,
            },
        )
        orders = (corpo.get("data") or {}).get("orders") or {}
        arestas = orders.get("edges") or []
        nos.extend(a.get("node") or {} for a in arestas)

        pagina = orders.get("pageInfo") or {}
        if not pagina.get("hasNextPage") or not arestas:
            break
        cursor = pagina.get("endCursor")
        print(f"  ... {len(nos)} pedidos")

    return nos


def relatar(nos: list[dict[str, Any]]) -> int:
    total = len(nos)
    if not total:
        print(f"  {ALERTA} nenhum pedido despachado no periodo.")
        print("      Amplie a janela com --dias, ou confira se a loja usa outro")
        print("      status de fulfillment.")
        return 1

    usaveis = 0
    por_fonte: Counter[str] = Counter()
    motivos: Counter[str] = Counter()
    sem_nome = 0
    # Se NENHUM pedido trouxer qualquer um dos tres campos, o mais provavel nao
    # e que a loja nao colete telefone -- e que o app nao enxergue o campo.
    algum_campo_veio = False

    for no in nos:
        entrega = no.get("shippingAddress") or {}

        candidatos = (
            ("shippingAddress.phone", entrega.get("phone")),
            ("order.phone", no.get("phone")),
        )
        if any(v for _, v in candidatos):
            algum_campo_veio = True

        for fonte, bruto in candidatos:
            if normalizar_telefone_br(bruto):
                usaveis += 1
                por_fonte[fonte] += 1
                break
        else:
            # Nenhuma fonte serviu: registra o porque da MELHOR candidata.
            preenchida = next((v for _, v in candidatos if v), None)
            motivos[classificar_falha(preenchida)] += 1

        if not (entrega.get("firstName") or "").strip():
            sem_nome += 1

    pct = 100.0 * usaveis / total
    print(f"\n  Pedidos despachados analisados : {total}")
    print(f"  Com telefone USAVEL (WhatsApp) : {usaveis}  ({pct:.1f}%)")
    print(f"  Sem contato utilizavel         : {total - usaveis}  ({100 - pct:.1f}%)")

    if por_fonte:
        print("\n  De onde veio o numero que serviu:")
        for fonte, n in por_fonte.most_common():
            print(f"    {n:5d}  {fonte}")
        print("\n  -> Esta ordem confirma a cadeia de fallback de `shopify.py`.")

    if motivos:
        print("\n  Por que os demais nao servem:")
        for motivo, n in motivos.most_common():
            print(f"    {n:5d}  {motivo}")

    print(f"\n  Sem primeiro nome (mensagem impessoal): {sem_nome}  "
          f"({100.0 * sem_nome / total:.1f}%)")

    print(f"\n{'=' * 70}\nVEREDITO\n{'=' * 70}")

    if not algum_campo_veio:
        print(f"  {ALERTA} NENHUM dos {total} pedidos trouxe telefone em campo algum.")
        print("      Isso quase nunca e a loja nao coletar telefone -- e o app")
        print("      nao ter acesso a PROTECTED CUSTOMER DATA. A Shopify devolve")
        print("      os campos como null em vez de negar a consulta.")
        print("      Va em: Shopify admin > Apps > seu app > API access >")
        print("      'Protected customer data access' e solicite o acesso.")
        return 1

    if pct >= 90:
        print(f"  {OK} Cobertura alta ({pct:.1f}%). A Shopify basta como fonte.")
        print("      A segunda fonte na Frete Rapido pode ficar como nota de rodape.")
    elif pct >= 60:
        print(f"  {ALERTA} Cobertura media ({pct:.1f}%). Funciona, mas cerca de")
        print(f"      {total - usaveis} de {total} clientes nao seriam avisados.")
        print("      Vale implementar `GET quote/{id_frete}` como segunda fonte.")
    else:
        print(f"  {FALHA} Cobertura baixa ({pct:.1f}%). So com a Shopify, a maioria")
        print("      dos clientes ficaria sem aviso -- e o recurso nao entrega o")
        print("      que promete. Implemente a segunda fonte ANTES de ligar o envio.")

    if any("nono" in m for m in motivos):
        n = sum(v for k, v in motivos.items() if "nono" in k)
        print(f"\n  {ALERTA} {n} numero(s) sao celular antigo de 8 digitos.")
        print("      Recusamos de proposito: prefixar o '9' inventaria um digito, e")
        print("      o numero resultante pode ser de outra pessoa. Se este bolo for")
        print("      grande, a conversa e com o cadastro da loja, nao com o codigo.")

    return 0


async def principal() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dias", type=int, default=30,
                    help="janela de pedidos (padrao: 30; sem read_all_orders o "
                         "teto real da Shopify e 60)")
    ap.add_argument("--max", dest="maximo", type=int, default=250,
                    help="maximo de pedidos a analisar (padrao: 250)")
    args = ap.parse_args()

    print(f"{'=' * 70}\nCOBERTURA DE TELEFONE -- pedidos despachados\n{'=' * 70}")

    try:
        nos = await coletar(args.dias, args.maximo)
    except ShopifyAcessoNegado as exc:
        detalhe = redigir_excecao(exc)
        print(f"\n  {FALHA} ACESSO NEGADO pela Shopify.")
        # A mensagem e truncada porque a Shopify repete o mesmo erro uma vez por
        # pedido da pagina -- 100 linhas identicas escondem o que importa.
        print(f"      {detalhe[:300]}")

        # Sao dois problemas diferentes, com solucoes diferentes, e confundi-los
        # custa dias esperando uma aprovacao que nao era necessaria.
        if "read_customers" in detalhe:
            print("\n      Causa: falta o ESCOPO `read_customers`.")
            print("      Nao e protected customer data -- e escopo comum.")
            print("\n      Mas nem precisa dele: o telefone e o nome saem do")
            print("      `shippingAddress`, que o `read_orders` ja alcanca.")
            print("      Se esta mensagem apareceu, a consulta deste script")
            print("      divergiu de `app/services/shopify.py`. Realinhe as duas.")
        else:
            print("\n      Causa provavel: PROTECTED CUSTOMER DATA -- telefone,")
            print("      nome e endereco exigem aprovacao separada do escopo.")
            print("\n      Shopify admin > Apps > seu app > API access >")
            print("      'Protected customer data access' > solicitar.")
            print("\n      Enquanto isso nao sair, o aviso proativo precisa buscar")
            print("      o telefone na Frete Rapido (`GET quote/{id_frete}`).")
        return 1
    except ShopifyErro as exc:
        print(f"\n  {FALHA} {redigir_excecao(exc)}")
        return 1

    return relatar(nos)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))
