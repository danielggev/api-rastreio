"""Descoberta do IP real do cliente atras do proxy reverso.

Este modulo existe por causa de uma falha silenciosa: com Caddy ou Nginx na
frente, `request.client.host` devolve o IP do PROXY (`127.0.0.1`) para todos os
visitantes. O rate limit passa a contar todo mundo no mesmo balde -- ou o
primeiro usuario bloqueia os demais, ou o limite vira inocuo. Como o rate
limiting e a defesa principal contra enumeracao de pedidos, isso desliga o
controle sem emitir erro nenhum.

A correcao NAO e simplesmente confiar em `X-Forwarded-For`: esse cabecalho e
enviado pelo cliente e pode ser forjado. Quem forja escapa do limite. So
aceitamos o cabecalho quando a conexao vem de um proxy declarado como confiavel.
"""

from __future__ import annotations

import ipaddress
import logging

from starlette.requests import Request

logger = logging.getLogger(__name__)

CABECALHO = "x-forwarded-for"


def _ip_valido(valor: str) -> str | None:
    try:
        return str(ipaddress.ip_address(valor.strip()))
    except ValueError:
        return None


def e_confiavel(ip: str, proxies_confiaveis: list[str]) -> bool:
    """Se a conexao veio de um proxy declarado confiavel.

    Aceita IP exato (`127.0.0.1`) ou FAIXA em notacao CIDR (`172.16.0.0/12`).
    A faixa existe por causa do Docker: o container do proxy recebe um IP
    dinamico na rede interna, e exigir IP exato quebraria a cada `docker compose
    up`. Sem isso, o rate limit passaria a contar todos os visitantes como um
    unico IP -- desligando o controle silenciosamente.
    """
    if not ip:
        return False

    try:
        endereco = ipaddress.ip_address(ip)
    except ValueError:
        return False

    for entrada in proxies_confiaveis:
        alvo = entrada.strip()
        if not alvo:
            continue
        if alvo == ip:
            return True
        try:
            if endereco in ipaddress.ip_network(alvo, strict=False):
                return True
        except ValueError:
            # Entrada malformada na configuracao: ignorar e seguir. Falhar aqui
            # derrubaria a requisicao por causa de um erro de digitacao no .env.
            logger.warning("entrada invalida em TRUSTED_PROXIES: %r", alvo)
    return False


def ip_do_cliente(request: Request, proxies_confiaveis: list[str]) -> str:
    """IP do cliente, considerando o proxy apenas quando ele e confiavel.

    Sem proxy confiavel configurado, o cabecalho e IGNORADO -- ainda que
    presente. Preferimos contar errado a ser contornado por um cabecalho forjado.
    """
    imediato = request.client.host if request.client else ""

    if not e_confiavel(imediato, proxies_confiaveis):
        if imediato and CABECALHO in request.headers:
            logger.debug(
                "X-Forwarded-For ignorado: conexao de %s nao e proxy confiavel",
                imediato,
            )
        return imediato or "desconhecido"

    encaminhado = request.headers.get(CABECALHO, "")
    if not encaminhado:
        return imediato or "desconhecido"

    # A cadeia e "cliente, proxy1, proxy2". O primeiro item e o cliente
    # original -- os seguintes foram acrescentados pelos proxies do caminho.
    for parte in encaminhado.split(","):
        ip = _ip_valido(parte)
        if ip:
            return ip

    return imediato or "desconhecido"
