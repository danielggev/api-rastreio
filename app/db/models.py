"""Modelos de persistencia.

Tres tabelas, com propositos bem separados:

- `consulta_log`: auditoria e deteccao de abuso. Contem dado pessoal
  PSEUDONIMIZADO e tem retencao de 90 dias.
- `rastreio_cache`: cache das ocorrencias da Frete Rapido. Guarda apenas o
  modelo normalizado, nunca o payload bruto -- o cru traz CPF/CNPJ e nome do
  entregador, dados pessoais de terceiros sem utilidade para a resposta.
- `evento_frete`: ocorrencias recebidas por webhook e o desfecho de cada uma.
  Sem NENHUM dado pessoal, nem pseudonimizado.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ConsultaLog(Base):
    """Registro de uma consulta de rastreio.

    NAO ha coluna de URL: o token da Frete Rapido trafega na query string, e uma
    URL gravada aqui vazaria o segredo para dentro do banco e dos backups.
    """

    __tablename__ = "consulta_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    # HMAC-SHA256 do email normalizado. NUNCA o email em claro.
    email_hmac: Mapped[str | None] = mapped_column(String(64), index=True)
    numero_pedido: Mapped[str | None] = mapped_column(String(64), index=True)

    ip_origem: Mapped[str | None] = mapped_column(String(64))
    # Cabecalho controlado pelo cliente: truncado na gravacao.
    user_agent: Mapped[str | None] = mapped_column(String(512))

    resultado: Mapped[str] = mapped_column(String(32), index=True)
    # Qual dos CNPJs atendeu, quando houve dados.
    cnpj: Mapped[str | None] = mapped_column(String(64))
    veio_do_cache: Mapped[bool] = mapped_column(default=False)
    anomalias: Mapped[list[str] | None] = mapped_column(JSON)
    latencia_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        # Suporta o alerta por TAXA de `vazio_fr` numa janela de tempo.
        Index("ix_consulta_log_resultado_criado", "resultado", "criado_em"),
    )


class RastreioCache(Base):
    __tablename__ = "rastreio_cache"

    numero_pedido: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Lista de ocorrencias JA NORMALIZADAS (lista de permissao aplicada).
    ocorrencias: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    cnpj: Mapped[str | None] = mapped_column(String(64))
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class EventoFrete(Base):
    """Uma ocorrencia recebida por webhook da Frete Rapido, e o que fizemos com ela.

    NAO ha coluna de telefone, nome ou email. O contato e lido da Shopify no
    momento do envio, repassado ao n8n e descartado -- esta tabela registra que
    o aviso saiu, nunca para quem. A propriedade "sem dado pessoal em repouso"
    vale aqui inteira, sem nem precisar de pseudonimizacao.

    A restricao UNIQUE e a defesa contra o pior modo de falha do projeto: a
    Frete Rapido reenvia o mesmo evento ate 12 vezes em ~24h enquanto nao
    receber HTTP 200, e sem ela o cliente receberia 12 mensagens identicas.
    """

    __tablename__ = "evento_frete"

    id: Mapped[int] = mapped_column(primary_key=True)
    numero_pedido: Mapped[str] = mapped_column(String(64), index=True)
    codigo: Mapped[int] = mapped_column(Integer)
    data_ocorrencia: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    grupo: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    tentativas: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    recebido_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    enviado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Mensagem ja redigida (`redigir_excecao`), truncada.
    erro: Mapped[str | None] = mapped_column(String(256))

    __table_args__ = (
        # NULLS NOT DISTINCT e obrigatorio, nao detalhe: por padrao o Postgres
        # trata cada NULL como distinto, entao duas reentregas do mesmo evento
        # SEM `data_ocorrencia` passariam pela restricao e virariam duas
        # mensagens. Exige PG15+; producao roda PG17.
        UniqueConstraint(
            "numero_pedido",
            "codigo",
            "data_ocorrencia",
            name="uq_evento_frete_ocorrencia",
            postgresql_nulls_not_distinct=True,
        ),
        # Sustenta a trava anti-spam, que conta avisos recentes por pedido.
        Index("ix_evento_frete_pedido_recebido", "numero_pedido", "recebido_em"),
        # Sustenta o relatorio de linhas presas em `pendente`.
        Index("ix_evento_frete_status_recebido", "status", "recebido_em"),
    )
