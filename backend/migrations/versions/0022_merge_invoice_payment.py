"""合并独立发布的发票抵扣规则与付款自动同意迁移。"""


revision = "0022_merge_invoice_payment"
down_revision = ("0020_payment_auto_approval", "0021_invoice_deduction_rules")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
