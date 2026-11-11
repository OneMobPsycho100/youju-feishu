"""区分报销与付款申请；规则版本 3 在保存配置时显式启用。"""
from alembic import op
import sqlalchemy as sa

revision = "0010_payment_workflow"
down_revision = "0009_configurable_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approval_templates", sa.Column("business_type", sa.String(24), nullable=False, server_default="REIMBURSEMENT"))
    op.add_column("approvals", sa.Column("business_type", sa.String(24), nullable=False, server_default="REIMBURSEMENT"))
    op.add_column("approvals", sa.Column("payment_details", sa.JSON(), nullable=False, server_default="{}"))
    op.create_index("ix_approvals_business_type", "approvals", ["business_type"])


def downgrade():
    op.drop_index("ix_approvals_business_type", table_name="approvals")
    op.drop_column("approvals", "payment_details")
    op.drop_column("approvals", "business_type")
    op.drop_column("approval_templates", "business_type")
