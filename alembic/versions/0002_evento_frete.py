"""evento_frete: ocorrencias recebidas por webhook da Frete Rapido

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evento_frete",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("numero_pedido", sa.String(64), nullable=False),
        sa.Column("codigo", sa.Integer(), nullable=False),
        sa.Column("data_ocorrencia", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grupo", sa.String(32), nullable=False),
        # descartado | observado | sem_contato | pendente | enviado
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "recebido_em",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("enviado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("erro", sa.String(256), nullable=True),
        # NULLS NOT DISTINCT e o que impede o cliente de receber a mesma
        # mensagem 12 vezes: a Frete Rapido reenvia o evento enquanto nao
        # receber HTTP 200, e sem isto duas reentregas sem `data_ocorrencia`
        # passariam pela restricao. Exige PostgreSQL 15+.
        sa.UniqueConstraint(
            "numero_pedido",
            "codigo",
            "data_ocorrencia",
            name="uq_evento_frete_ocorrencia",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_evento_frete_numero_pedido", "evento_frete", ["numero_pedido"])
    op.create_index("ix_evento_frete_grupo", "evento_frete", ["grupo"])
    op.create_index("ix_evento_frete_status", "evento_frete", ["status"])
    op.create_index("ix_evento_frete_recebido_em", "evento_frete", ["recebido_em"])
    # Sustenta a trava anti-spam, que conta avisos recentes por pedido.
    op.create_index(
        "ix_evento_frete_pedido_recebido", "evento_frete", ["numero_pedido", "recebido_em"]
    )
    # Sustenta o relatorio de linhas presas em `pendente`.
    op.create_index(
        "ix_evento_frete_status_recebido", "evento_frete", ["status", "recebido_em"]
    )


def downgrade() -> None:
    op.drop_table("evento_frete")
