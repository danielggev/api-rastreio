"""Validacao contra as APIs REAIS -- Camada 4 do plano.

Roda manualmente, fora do CI, porque exige segredos:

    .venv\\Scripts\\python.exe scripts/validar_integracoes.py

A API e SOMENTE LEITURA nas duas integracoes: nada e criado, alterado ou
cancelado. Rodar contra producao nao corrompe dado nenhum.

Nenhum segredo e impresso: URLs e excecoes passam pela redacao, e o email do
pedido aparece mascarado.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.services.cache import CacheDesligado
from app.services.consulta import ServicoConsulta
from app.services.frete_rapido import ClienteFreteRapido
from app.services.logs import redigir_excecao
from app.services.multi_cnpj import BuscadorMultiCNPJ
from app.services.normalizacao import NumeroPedidoFR, montar_name_shopify
from app.services.ocorrencias import classificar
from app.services.ordenacao import ordenar_desc
from app.services.shopify import ClienteShopify

OK = "[ok]"
FALHA = "[falha]"
ALERTA = "[!]"


def mascarar_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "(vazio)"
    usuario, dominio = email.split("@", 1)
    return f"{usuario[:2]}***@{dominio[:1]}***"


def titulo(texto: str) -> None:
    print(f"\n{'=' * 70}\n{texto}\n{'=' * 70}")


async def validar_shopify(numero: NumeroPedidoFR) -> object | None:
    titulo("1. SHOPIFY -- unico ponto ainda nao verificado contra a API real")
    cliente = ClienteShopify()
    print(f"  URL      : {cliente.url}")
    print(f"  Buscando : name = {montar_name_shopify(numero)!r}")

    try:
        pedido = await cliente.buscar_pedido(numero)
    except Exception as exc:
        print(f"  {FALHA} {redigir_excecao(exc)}")
        return None

    if pedido is None:
        print(f"  {FALHA} nenhuma correspondencia EXATA de `name`.")
        print("        Possiveis causas: prefixo/sufixo configurado diferente,")
        print("        pedido com mais de 60 dias (sem read_all_orders), ou")
        print("        numero inexistente nesta loja.")
        return None

    print(f"  {OK} pedido encontrado")
    print(f"       name           : {pedido.name}")
    print(f"       email          : {mascarar_email(pedido.email_normalizado)}")
    print(f"       criado em      : {pedido.criado_em}")
    print(f"       tem fulfillment: {pedido.tem_fulfillment}")
    print(f"       rastreio       : {pedido.codigo_rastreio or '(nenhum)'}")
    print(f"       tags           : {pedido.tags or '(nenhuma)'}")
    if pedido.anomalias:
        print(f"  {ALERTA} anomalias: {[a.value for a in pedido.anomalias]}")
    return pedido


async def validar_frete_rapido(
    numero: NumeroPedidoFR, tags: list[str]
) -> list[object]:
    titulo("2. FRETE RAPIDO -- Estrategia A com selecao de CNPJ pela tag")
    s = get_settings()
    tokens = s.tokens_frete_rapido
    print(f"  CNPJs configurados : {list(tokens.keys())}")
    print(f"  Tags do pedido     : {tags or '(nenhuma)'}")

    reconhecida = [t for t in tags if t in tokens]
    if reconhecida:
        print(f"  {OK} tag reconhecida: {reconhecida[0]} -> caminho rapido")
    else:
        print(f"  {ALERTA} nenhuma tag casa com os CNPJs configurados.")
        print("        A busca vai consultar TODOS os tokens em paralelo.")
        print("        Verifique se as chaves de FRETE_RAPIDO_TOKENS batem com")
        print("        as tags reais do pedido.")

    buscador = BuscadorMultiCNPJ(ClienteFreteRapido(), tokens)
    try:
        resultado = await buscador.buscar(numero, tags)
    except Exception as exc:
        print(f"  {FALHA} {redigir_excecao(exc)}")
        return []

    if resultado.anomalias:
        print(f"  {ALERTA} anomalias: {[a.value for a in resultado.anomalias]}")
    if resultado.houve_falha:
        print(f"  {ALERTA} ao menos um CNPJ falhou na consulta.")

    if not resultado.ocorrencias:
        print(f"  {ALERTA} lista VAZIA em todos os CNPJs consultados.")
        print("        Se este pedido ja foi despachado, e o sinal `vazio_fr`.")
        return []

    print(f"  {OK} {len(resultado.ocorrencias)} ocorrencia(s) no CNPJ {resultado.cnpj}")
    ordenadas = ordenar_desc(resultado.ocorrencias)
    for o in ordenadas:
        grupo = classificar(o.codigo)
        print(f"       [{o.codigo:>3}] {o.data_ocorrencia}  {o.nome}  -> {grupo.value}")

    print(f"\n       codigo_volume      : {ordenadas[0].codigo_volume or '(ausente)'}")
    return list(ordenadas)


def validar_fuso(ordenadas: list[object]) -> None:
    """Pendencia do plano: as datas sao horario de Brasilia ou UTC?

    Desvio de exatamente 3 horas entre a ocorrencia mais recente e o relogio
    denuncia UTC.
    """
    titulo("3. FUSO HORARIO -- premissa a confirmar")
    if not ordenadas:
        print(f"  {ALERTA} sem ocorrencias para comparar.")
        return

    mais_recente = ordenadas[0].data_ocorrencia  # type: ignore[attr-defined]
    if mais_recente is None:
        print(f"  {ALERTA} ocorrencia mais recente sem data.")
        return

    agora_sp = datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
    agora_utc = datetime.now(UTC).replace(tzinfo=None)

    print(f"  ocorrencia mais recente : {mais_recente}")
    print(f"  agora em Sao Paulo      : {agora_sp:%Y-%m-%d %H:%M:%S}")
    print(f"  agora em UTC            : {agora_utc:%Y-%m-%d %H:%M:%S}")
    print(f"  diferenca p/ Sao Paulo  : {(agora_sp - mais_recente).total_seconds() / 3600:+.1f} h")
    print(f"  diferenca p/ UTC        : {(agora_utc - mais_recente).total_seconds() / 3600:+.1f} h")
    print("\n  Interpretacao: conclusivo apenas com uma ocorrencia RECENTE.")
    print("  Se a diferenca para Sao Paulo for ~0, as datas sao horario de Brasilia.")


async def validar_fluxo(email: str, numero_digitado: str) -> None:
    titulo("4. FLUXO COMPLETO -- ponta a ponta")
    s = get_settings()
    servico = ServicoConsulta(
        ClienteShopify(),
        BuscadorMultiCNPJ(ClienteFreteRapido(), s.tokens_frete_rapido),
        CacheDesligado(),
    )

    consulta = await servico.consultar(email, numero_digitado)
    print(f"  resultado : {consulta.resultado.value}  (HTTP {consulta.status_http})")
    print(f"  resposta  : {consulta.resposta.model_dump_json(indent=2)}")

    titulo("5. VAZAMENTO DE EXISTENCIA -- email errado deve responder identico")
    errado = await servico.consultar("nao-existe@exemplo-invalido.com", numero_digitado)
    inexistente = await servico.consultar(email, "99999999")

    if errado.resposta.model_dump_json() == inexistente.resposta.model_dump_json():
        print(f"  {OK} email errado e pedido inexistente respondem identico")
    else:
        print(f"  {FALHA} respostas divergem -- permite enumerar pedidos validos")


async def principal() -> int:
    s = get_settings()

    titulo("CONFIGURACAO")
    print(f"  ENV                 : {s.env}")
    print(f"  Loja                : {s.shopify_shop_domain or '(vazio)'}")
    print(f"  Versao Shopify      : {s.shopify_api_version}")
    print(f"  Prefixo / sufixo    : {s.shopify_order_prefix!r} / {s.shopify_order_suffix!r}")
    print(f"  Token Shopify       : {'definido' if s.shopify_access_token else 'AUSENTE'}")
    tokens = s.tokens_frete_rapido
    print(f"  CNPJs Frete Rapido  : {list(tokens.keys()) if tokens else 'AUSENTE'}")
    print(f"  Fuso atribuido      : {s.frete_rapido_timezone}")

    # Permite validar outro pedido sem editar o .env:
    #   python scripts/validar_integracoes.py 59551 [email]
    numero_bruto = sys.argv[1] if len(sys.argv) > 1 else s.contract_test_order_number
    email = sys.argv[2] if len(sys.argv) > 2 else s.contract_test_email
    if not numero_bruto or not email:
        print(
            f"\n{FALHA} preencha CONTRACT_TEST_ORDER_NUMBER e CONTRACT_TEST_EMAIL no .env\n"
            "        (numero de um pedido RECENTE e o email usado na compra)"
        )
        return 1

    try:
        numero = NumeroPedidoFR(numero_bruto)
    except ValueError as exc:
        print(f"\n{FALHA} {exc}")
        return 1

    print(f"  Pedido de teste     : {numero_bruto!r} -> normalizado {numero!r}")

    pedido = await validar_shopify(numero)
    tags = list(getattr(pedido, "tags", []) or [])
    ordenadas = await validar_frete_rapido(numero, tags)
    validar_fuso(ordenadas)
    await validar_fluxo(email, numero_bruto)

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))
