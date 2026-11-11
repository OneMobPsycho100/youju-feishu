"""增加按模板开启 AI 预审的开关，迁移后由管理员逐个启用。"""
from alembic import op
import sqlalchemy as sa


revision = "0018_template_ai_approval"
down_revision = "0017_template_pre_review_nodes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approval_templates", sa.Column("ai_approval_enabled", sa.Boolean(), nullable=False,
        server_default=sa.false(), comment="是否允许 AI 预审及按规则自动回写"))
    op.execute("UPDATE approval_templates SET mode = 'OBSERVE' WHERE mode = 'AUTO'")


def downgrade():
    op.drop_column("approval_templates", "ai_approval_enabled")
