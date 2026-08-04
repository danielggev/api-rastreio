"""evento_frete: fencing do lease, cooldown e cota de aviso separada

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-04

Tres correcoes da segunda rodada de revisao de seguranca:

- `processando_por`: sem dono, um worker com lease VENCIDO ainda concluia e
  apagava o lease de quem assumiu depois -- o UPDATE filtrava so pela chave.
- `proxima_tentativa_em`: o teto de tentativas so valia para linha nova.
  Repetir a MESMA linha pendente passava direto e reconsultava a Frete Rapido
  a cada vez, sem limite.
- `aviso_reservado_em`: a cota de mensagens era contada ANTES da confirmacao,
  entao eventos forjados ocupavam vaga e barravam avisos legitimos.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evento_frete", sa.Column("processando_por", sa.String(64), nullable=True)
    )
    op.add_column(
        "evento_frete",
        sa.Column("proxima_tentativa_em", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "evento_frete",
        sa.Column("aviso_reservado_em", sa.DateTime(timezone=True), nullable=True),
    )
    # Sustenta a contagem da cota de mensagens, que agora filtra por este campo.
    op.create_index(
        "ix_evento_frete_aviso_reservado",
        "evento_frete",
        ["numero_pedido", "aviso_reservado_em"],
    )


def downgrade() -> None:
    op.drop_index("ix_evento_frete_aviso_reservado", table_name="evento_frete")
    op.drop_column("evento_frete", "aviso_reservado_em")
    op.drop_column("evento_frete", "proxima_tentativa_em")
    op.drop_column("evento_frete", "processando_por")
