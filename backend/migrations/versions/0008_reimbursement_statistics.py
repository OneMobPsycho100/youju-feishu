"""补存报销提交时间和人员身份，供独立统计页面使用。"""
from alembic import op
import sqlalchemy as sa

revision = "0008_reimbursement_statistics"
down_revision = "0007_reimbursement_review"
branch_labels = None
depends_on = None


def upgrade():
    # 原记录保留 NULL，由后台从权威实例补齐，迁移内不进行网络请求或伪造业务时间。
    op.add_column("approvals", sa.Column("submitted_at", sa.DateTime(), nullable=True))
    op.add_column("approvals", sa.Column("applicant_id", sa.String(120), nullable=True))
    op.create_index("ix_approvals_submitted_at", "approvals", ["submitted_at"])
    op.create_index("ix_approvals_applicant_id", "approvals", ["applicant_id"])


def downgrade():
    op.drop_index("ix_approvals_applicant_id", table_name="approvals")
    op.drop_index("ix_approvals_submitted_at", table_name="approvals")
    op.drop_column("approvals", "applicant_id")
    op.drop_column("approvals", "submitted_at")
