"""保存每个公司主体的发票抵扣规则。"""
from alembic import op
import sqlalchemy as sa


revision = "0021_invoice_deduction_rules"
down_revision = "0019_reimbursement_budgets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "invoice_deduction_configs",
        sa.Column("company_id", sa.String(32), sa.ForeignKey("companies.id"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0", comment="配置乐观锁版本"),
        sa.Column("rules", sa.JSON(), nullable=False, server_default="[]", comment="票种、费用类型和抵扣方式"),
    )


def downgrade():
    op.drop_table("invoice_deduction_configs")
