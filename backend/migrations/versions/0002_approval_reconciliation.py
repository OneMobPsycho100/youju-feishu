"""为存量审批增加轮询游标，补偿旧单据的终态和撤销事件。"""
from alembic import op
import sqlalchemy as sa

revision = "0002_reconciliation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approvals", sa.Column("last_synced_at", sa.DateTime(), nullable=False,
        server_default=sa.text("'1970-01-01 00:00:00'"), comment="最近一次权威状态读取，用于存量轮询补偿"))
    # SQLite 不允许通过 ALTER ADD COLUMN 添加动态时间默认值，先用常量再回填。
    op.execute("UPDATE approvals SET last_synced_at = updated_at")
    op.create_index("ix_approvals_last_synced_at", "approvals", ["last_synced_at"])


def downgrade():
    op.drop_index("ix_approvals_last_synced_at", table_name="approvals")
    op.drop_column("approvals", "last_synced_at")
