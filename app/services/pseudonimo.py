"""Pseudonimizacao do email para o log de auditoria.

O log existe para dar suporte e detectar abuso -- nao para guardar a lista de
quem consultou o que. Gravamos apenas um HMAC do email.

Por que HMAC e nao SHA-256 com salt concatenado: HMAC e a construcao correta
para autenticacao de mensagem com chave, e nao sofre de extension attacks. Com
salt concatenado, o valor so protege enquanto o salt for secreto -- e a
implementacao ingenua (`sha256(salt + email)`) tem fraquezas conhecidas.

IMPORTANTE, sob a LGPD: isto e PSEUDONIMIZACAO, nao anonimizacao. Enquanto a
chave existir, o valor continua sendo dado pessoal -- e o universo de emails e
pequeno o bastante para que qualquer um com a chave reverta por forca bruta.
Numero do pedido, IP e user-agent tambem sao dados pessoais quando combinados.
Tratar o conjunto como dado pessoal.
"""

from __future__ import annotations

import hashlib
import hmac

from app.config import get_settings
from app.services.normalizacao import normalizar_email


def hmac_email(bruto: str | None, chave: str | None = None) -> str | None:
    """HMAC-SHA256 do email normalizado. `None` quando nao ha email.

    Usa a MESMA funcao de normalizacao da comparacao. Se as duas divergissem, o
    mesmo email geraria hashes diferentes e a correlacao dos logs quebraria sem
    nenhum sintoma visivel.
    """
    normalizado = normalizar_email(bruto)
    if normalizado is None:
        return None

    segredo = chave if chave is not None else get_settings().email_hmac_key
    if not segredo:
        raise ValueError("EMAIL_HMAC_KEY nao configurada")

    return hmac.new(
        segredo.encode("utf-8"),
        normalizado.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
