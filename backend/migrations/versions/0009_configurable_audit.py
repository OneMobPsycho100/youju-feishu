"""自动审批操作绑定审核快照；旧模板策略不在迁移时自动升级。"""
from alembic import op

revision = "0009_configurable_audit"
down_revision = "0008_reimbursement_statistics"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite 允许添加可空外键列，避免重建父表影响既有操作和审核留痕。
    # Alembic 对带 ForeignKey 的 add_column 会拆成不受 SQLite 支持的 ADD CONSTRAINT，
    # 因此在同一条 ADD COLUMN 中定义 REFERENCES，保留真实外键而不重建表。
    op.execute("ALTER TABLE approval_actions ADD COLUMN source_audit_id VARCHAR(32) REFERENCES audit_runs(id)")


def downgrade():
    op.drop_column("approval_actions", "source_audit_id")
