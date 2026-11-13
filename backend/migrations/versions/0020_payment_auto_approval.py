"""付款默认仅采集并放行，材料采集及回写来源分别留痕。"""
from alembic import op
import sqlalchemy as sa


revision = "0020_payment_auto_approval"
down_revision = "0019_reimbursement_budgets"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approval_templates", sa.Column("payment_auto_approve_enabled", sa.Boolean(),
        nullable=False, server_default=sa.false(), comment="付款仅采集并自动同意专用节点"))
    op.execute("UPDATE approval_templates SET payment_auto_approve_enabled = 1 WHERE business_type = 'PAYMENT'")
    op.add_column("approvals", sa.Column("payment_collection_task_id", sa.String(32), nullable=True,
        comment="当前付款材料采集后台任务 ID"))
    op.add_column("approvals", sa.Column("payment_collection_fingerprint", sa.String(64),
        nullable=False, server_default="", comment="独立采集内容与识别配置指纹"))
    op.add_column("approval_actions", sa.Column("execution_source", sa.String(32),
        nullable=False, server_default="STANDARD", comment="STANDARD / PAYMENT_AUTO_APPROVE"))


def downgrade():
    op.drop_column("approval_actions", "execution_source")
    op.drop_column("approvals", "payment_collection_fingerprint")
    op.drop_column("approvals", "payment_collection_task_id")
    op.drop_column("approval_templates", "payment_auto_approve_enabled")
