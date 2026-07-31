"""estrutura inicial: consulta_log e rastreio_cache

Revision ID: 0001
Revises:
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consulta_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # HMAC-SHA256 do email normalizado. NUNCA o email em claro.
        sa.Column("email_hmac", sa.String(64), nullable=True),
        sa.Column("numero_pedido", sa.String(64), nullable=True),
        sa.Column("ip_origem", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("resultado", sa.String(32), nullable=False),
        sa.Column("cnpj", sa.String(64), nullable=True),
        sa.Column("veio_do_cache", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("anomalias", sa.JSON(), nullable=True),
        sa.Column("latencia_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_consulta_log_criado_em", "consulta_log", ["criado_em"])
    op.create_index("ix_consulta_log_email_hmac", "consulta_log", ["email_hmac"])
    op.create_index("ix_consulta_log_numero_pedido", "consulta_log", ["numero_pedido"])
    op.create_index("ix_consulta_log_resultado", "consulta_log", ["resultado"])
    # Suporta o alerta por TAXA de `vazio_fr` numa janela de tempo.
    op.create_index(
        "ix_consulta_log_resultado_criado", "consulta_log", ["resultado", "criado_em"]
    )

    op.create_table(
        "rastreio_cache",
        sa.Column("numero_pedido", sa.String(64), primary_key=True),
        # Ocorrencias JA NORMALIZADAS: o payload bruto traz CPF/CNPJ e nome do
        # entregador, dados pessoais de terceiros sem utilidade aqui.
        sa.Column("ocorrencias", sa.JSON(), nullable=False),
        sa.Column("cnpj", sa.String(64), nullable=True),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_rastreio_cache_atualizado_em", "rastreio_cache", ["atualizado_em"]
    )


def downgrade() -> None:
    op.drop_table("rastreio_cache")
    op.drop_table("consulta_log")
