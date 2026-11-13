"""增加部门预算、每单用量快照和操作留痕；历史建账由本地后台任务完成。"""
from alembic import op
import sqlalchemy as sa

revision = "0019_reimbursement_budgets"
down_revision = "0018_template_ai_approval"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("budgets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("company_id", sa.String(32), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("department", sa.String(100), nullable=False),
        sa.Column("expense_type", sa.String(80), nullable=False),
        sa.Column("period_type", sa.String(16), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False), sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("warning_threshold", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False), sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("company_id", "department", "expense_type", "start_date", "end_date", name="uq_budget_scope_period"))
    for name in ("company_id", "start_date", "end_date"):
        op.create_index("ix_budgets_" + name, "budgets", [name])
    op.create_table("budget_usages",
        sa.Column("approval_id", sa.String(64), sa.ForeignKey("approvals.id"), primary_key=True),
        sa.Column("company_id", sa.String(32), sa.ForeignKey("companies.id"), nullable=True),
        sa.Column("department", sa.String(100), nullable=False), sa.Column("expense_type", sa.String(80), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=True), sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False), sa.Column("attribution_pending", sa.Boolean(), nullable=False),
        sa.Column("attribution_reason", sa.String(500), nullable=False), sa.Column("attribution_source", sa.String(16), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False), sa.Column("approved_snapshot", sa.JSON(), nullable=False),
        sa.Column("freeze_fingerprint", sa.String(64), nullable=False), sa.Column("released_fingerprint", sa.String(64), nullable=False),
        sa.Column("historical_review", sa.Boolean(), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False))
    for name in ("company_id", "submitted_at", "state", "attribution_pending"):
        op.create_index("ix_budget_usages_" + name, "budget_usages", [name])
    op.create_index("ix_budget_usages_scope", "budget_usages", ["company_id", "department", "submitted_at"])
    op.create_table("budget_operations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("budget_id", sa.String(32), sa.ForeignKey("budgets.id"), nullable=True),
        sa.Column("approval_id", sa.String(64), sa.ForeignKey("approvals.id"), nullable=True),
        sa.Column("action", sa.String(32), nullable=False), sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("actor_name", sa.String(100), nullable=False), sa.Column("reason", sa.String(600), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False), sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("request_id", sa.String(100), nullable=True, unique=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    for name in ("budget_id", "approval_id", "created_at"):
        op.create_index("ix_budget_operations_" + name, "budget_operations", [name])


def downgrade():
    op.drop_table("budget_operations")
    op.drop_table("budget_usages")
    op.drop_table("budgets")
