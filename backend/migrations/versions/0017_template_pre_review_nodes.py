"""为审批模板增加多个专用预审节点，保留原节点作为兼容首项。"""
from alembic import op
import sqlalchemy as sa


revision = "0017_template_pre_review_nodes"
down_revision = "0016_approval_archive_print"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approval_templates", sa.Column("node_ids", sa.JSON(), nullable=False, server_default="[]",
        comment="专用预审节点 ID 列表，所有节点共用模板的专用审批人"))
    op.execute("UPDATE approval_templates SET node_ids = json_array(node_id)")


def downgrade():
    op.drop_column("approval_templates", "node_ids")
