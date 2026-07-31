"""Registro de auditoria das consultas.

O log serve para dar suporte e para detectar abuso. Duas regras nao negociaveis:

1. O email entra apenas como HMAC.
2. Nenhuma URL e gravada -- o token da Frete Rapido trafega na query string, e
   uma URL aqui vazaria o segredo para o banco e para os backups.

Falha ao registrar NUNCA derruba a consulta: o cliente nao pode ficar sem
resposta porque o banco esta indisponivel.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import ConsultaLog
from app.db.session import sessao
from app.services.consulta import Consulta
from app.services.normalizacao import truncar
from app.services.pseudonimo import hmac_email

logger = logging.getLogger(__name__)

RETENCAO_DIAS = 90


class Auditoria:
    def __init__(self, fabrica: async_sessionmaker[AsyncSession] | None) -> None:
        self._fabrica = fabrica

    async def registrar(
        self,
        consulta: Consulta,
        *,
        email: str | None,
        numero_pedido: str | None,
        ip: str | None,
        user_agent: str | None,
        latencia_ms: int | None = None,
    ) -> None:
        if self._fabrica is None:
            return

        s = get_settings()
        try:
            registro = ConsultaLog(
                email_hmac=hmac_email(email),
                numero_pedido=truncar(numero_pedido, 64),
                ip_origem=truncar(ip, 64),
                user_agent=truncar(user_agent, s.max_user_agent),
                resultado=consulta.resultado.value,
                cnpj=consulta.cnpj,
                veio_do_cache=consulta.veio_do_cache,
                anomalias=[a.value for a in consulta.anomalias] or None,
                latencia_ms=latencia_ms,
            )
            async with sessao(self._fabrica) as sess:
                sess.add(registro)
        except Exception as exc:
            # Auditoria e importante, mas nao mais que responder ao cliente.
            logger.error("falha ao registrar consulta no log: %s", exc)

    async def expurgar(self, dias: int = RETENCAO_DIAS) -> int:
        """Apaga registros alem da retencao. Devolve quantos foram removidos.

        A retencao so se completa de fato apos o ciclo de backup: copias antigas
        ainda contem os registros apagados aqui.
        """
        if self._fabrica is None:
            return 0

        limite = datetime.now(UTC) - timedelta(days=dias)
        async with sessao(self._fabrica) as sess:
            resultado = await sess.execute(
                delete(ConsultaLog).where(ConsultaLog.criado_em < limite)
            )
        removidos = int(getattr(resultado, "rowcount", 0) or 0)
        if removidos:
            logger.info("expurgo LGPD: %d registros com mais de %dd", removidos, dias)
        return removidos

    async def taxa_de(
        self, resultado: str, janela_horas: int = 1
    ) -> tuple[int, int]:
        """(ocorrencias, total) do resultado na janela.

        Alimenta o alerta por TAXA: `vazio_fr` isolado e normal -- atraso de
        propagacao entre transportadora e Frete Rapido acontece. E a taxa que
        denuncia bug sistematico, e um alerta por evento seria ruido que logo
        passaria a ser ignorado.
        """
        if self._fabrica is None:
            return (0, 0)

        desde = datetime.now(UTC) - timedelta(hours=janela_horas)
        async with sessao(self._fabrica) as sess:
            total = await sess.scalar(
                select(func.count())
                .select_from(ConsultaLog)
                .where(ConsultaLog.criado_em >= desde)
            )
            alvo = await sess.scalar(
                select(func.count())
                .select_from(ConsultaLog)
                .where(
                    ConsultaLog.criado_em >= desde,
                    ConsultaLog.resultado == resultado,
                )
            )
        return (int(alvo or 0), int(total or 0))
