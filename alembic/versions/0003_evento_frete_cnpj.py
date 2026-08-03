"""evento_frete: coluna cnpj, para saber qual cadastro do Dash FR originou o evento

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-03

O cadastro de webhook na Frete Rapido e POR CNPJ, e o payload nao diz de qual
embarcador veio -- traz apenas o CNPJ da transportadora. A origem passa a ser
deduzida do segredo da URL, com um segredo distinto por cadastro.

Nullable de proposito: as linhas ja gravadas com o segredo unico anterior nao
tem como ganhar a informacao retroativamente, e inventar um valor seria pior que
admitir que nao sabemos.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evento_frete", sa.Column("cnpj", sa.String(64), nullable=True))
    op.create_index("ix_evento_frete_cnpj", "evento_frete", ["cnpj"])
    # Sustenta o relatorio que denuncia um CNPJ que parou de enviar.
    op.create_index(
        "ix_evento_frete_cnpj_recebido", "evento_frete", ["cnpj", "recebido_em"]
    )


def downgrade() -> None:
    op.drop_index("ix_evento_frete_cnpj_recebido", table_name="evento_frete")
    op.drop_index("ix_evento_frete_cnpj", table_name="evento_frete")
    op.drop_column("evento_frete", "cnpj")
