"""Índice em transactions.created_at para as janelas de tempo do GET /stats.

O /stats filtra só por `created_at`; o índice (status, created_at) não serve a essa consulta
e ela virava varredura da tabela inteira a cada atualização do painel.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tabela grande em produção: trocar por CREATE INDEX CONCURRENTLY (fora de transação,
    # com `op.get_context().autocommit_block()`); ver docs/KNOWN_ISSUES.md.
    op.create_index("ix_transactions_created", "transactions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_transactions_created", table_name="transactions")
