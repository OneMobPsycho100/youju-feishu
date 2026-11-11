"""增加审批级归档所需的最终通过时间和打印快照。"""
from alembic import op
import sqlalchemy as sa


revision = "0016_approval_archive_print"
down_revision = "0015_approval_ai_status"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approvals", sa.Column("approved_at", sa.DateTime(), nullable=True,
        comment="飞书最终通过时间，UTC；历史缺失时留空"))
    op.create_index("ix_approvals_approved_at", "approvals", ["approved_at"])
    op.add_column("approvals", sa.Column("archive_snapshot", sa.JSON(), nullable=False, server_default="{}",
        comment="打印用审批轨迹及全单附件索引；补充原件不参与预审"))


def downgrade():
    op.drop_column("approvals", "archive_snapshot")
    op.drop_index("ix_approvals_approved_at", table_name="approvals")
    op.drop_column("approvals", "approved_at")
