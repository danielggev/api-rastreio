"""evento_frete: lease de processamento, para nao enviar duas vezes

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-03

`status = pendente` significava duas coisas ao mesmo tempo -- "alguem esta
processando" e "pode tentar de novo". Duas entregas simultaneas do mesmo evento
passavam as duas pela reserva e chamavam o n8n em duplicidade: a restricao
UNIQUE arbitra quem cria a LINHA, nao quem executa o EFEITO.

`processando_ate` e o lease. Expira sozinho, para que um processo que morra no
meio nao deixe o evento preso.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evento_frete",
        sa.Column("processando_ate", sa.DateTime(timezone=True), nullable=True),
    )
    # Sustenta a aquisicao do lease, que filtra por status + lease vencido.
    op.create_index(
        "ix_evento_frete_lease", "evento_frete", ["status", "processando_ate"]
    )


def downgrade() -> None:
    op.drop_index("ix_evento_frete_lease", table_name="evento_frete")
    op.drop_column("evento_frete", "processando_ate")
