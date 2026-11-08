"""实盘账本幂等、公司行动与自动研究调度键。

Revision ID: 20261108_01
Revises: None
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20261108_01"
down_revision = None
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "live_fill" in tables and "external_id" not in _columns(bind, "live_fill"):
        op.add_column("live_fill", sa.Column("external_id", sa.String(128), nullable=True))
    if "live_fill" in tables and "ux_live_fill_external" not in _indexes(bind, "live_fill"):
        op.create_index(
            "ux_live_fill_external", "live_fill",
            ["strategy_key", "source", "external_id"], unique=True,
            sqlite_where=sa.text("external_id IS NOT NULL"),
        )

    if "live_corporate_action" not in tables:
        op.create_table(
            "live_corporate_action",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("strategy_key", sa.String(64), nullable=False),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("share_ratio", sa.Float(), nullable=False, server_default="1"),
            sa.Column("cash_dividend_per_share", sa.Float(), nullable=False, server_default="0"),
            sa.Column("source", sa.String(12), nullable=False, server_default="manual"),
            sa.Column("external_id", sa.String(128), nullable=True),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("strategy_key", "source", "external_id",
                                name="uq_live_ca_external"),
        )
    elif "external_id" not in _columns(bind, "live_corporate_action"):
        op.add_column("live_corporate_action",
                      sa.Column("external_id", sa.String(128), nullable=True))
    if "ux_live_ca_external" not in _indexes(bind, "live_corporate_action"):
        op.create_index(
            "ux_live_ca_external", "live_corporate_action",
            ["strategy_key", "source", "external_id"], unique=True,
            sqlite_where=sa.text("external_id IS NOT NULL"),
        )

    if "auto_research_runs" in tables:
        cols = _columns(bind, "auto_research_runs")
        if "schedule_key" not in cols:
            op.add_column("auto_research_runs",
                          sa.Column("schedule_key", sa.String(32), nullable=True))
        if "attempt" not in cols:
            op.add_column("auto_research_runs",
                          sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
        if "ux_auto_research_schedule" not in _indexes(bind, "auto_research_runs"):
            op.create_index(
                "ux_auto_research_schedule", "auto_research_runs",
                ["job", "schedule_key"], unique=True,
                sqlite_where=sa.text("schedule_key IS NOT NULL"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "auto_research_runs" in tables:
        if "ux_auto_research_schedule" in _indexes(bind, "auto_research_runs"):
            op.drop_index("ux_auto_research_schedule", table_name="auto_research_runs")
        cols = _columns(bind, "auto_research_runs")
        if "attempt" in cols:
            op.drop_column("auto_research_runs", "attempt")
        if "schedule_key" in cols:
            op.drop_column("auto_research_runs", "schedule_key")
    if "live_corporate_action" in tables:
        op.drop_table("live_corporate_action")
    if "live_fill" in tables:
        if "ux_live_fill_external" in _indexes(bind, "live_fill"):
            op.drop_index("ux_live_fill_external", table_name="live_fill")
        if "external_id" in _columns(bind, "live_fill"):
            op.drop_column("live_fill", "external_id")
