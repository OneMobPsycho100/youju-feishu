"""金额从 NUMERIC 元转换为 BIGINT 分，保留已有 SQLite 开发库的升级路径。"""
from decimal import Decimal, ROUND_HALF_EVEN

from alembic import op
import sqlalchemy as sa

revision = "0005_sqlite_money"
down_revision = "0004_ai_call_logs"
branch_labels = None
depends_on = None


def convert_amounts(*, to_cents: bool):
    connection = op.get_bind()
    for table in ("approvals", "invoices"):
        target_type = sa.BigInteger() if to_cents else sa.Numeric(18, 2)
        op.add_column(table, sa.Column("amount_converted", target_type, nullable=False, server_default="0"))
        # 不重建父表，避免 DROP TABLE 触发附件、占用等外键；SQLite 原生支持删除及重命名列。
        rows = connection.execute(sa.text(f"SELECT id, amount FROM {table}")).all()
        for row in rows:
            if to_cents:
                amount = Decimal(str(row.amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
                value = int(amount * 100)
            else:
                value = str(Decimal(row.amount) / 100)
            connection.execute(sa.text(f"UPDATE {table} SET amount_converted = :amount WHERE id = :id"),
                               {"amount": value, "id": row.id})
        op.drop_column(table, "amount")
        op.alter_column(table, "amount_converted", new_column_name="amount")


def upgrade():
    convert_amounts(to_cents=True)


def downgrade():
    convert_amounts(to_cents=False)
