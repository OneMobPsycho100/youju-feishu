"""私人 Agent 会话附件及隔离的附件模型调用归属；仅增量建表，不改业务发票。"""
from alembic import op
import sqlalchemy as sa

revision = "0013_agent_attachments"
down_revision = "0012_feishu_web_configuration"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_attachments",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("conversation_id", sa.String(32), sa.ForeignKey("agent_conversations.id"), nullable=False),
        sa.Column("upload_id", sa.String(100), nullable=False),
        sa.Column("filename", sa.String(240), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error", sa.String(600), nullable=False),
        sa.Column("segments", sa.JSON(), nullable=False),
        sa.Column("recognition", sa.JSON(), nullable=False),
        sa.Column("submitted", sa.Boolean(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("conversation_id", "upload_id", name="uq_agent_attachment_upload"))
    for column in ("conversation_id", "sha256", "status"):
        op.create_index("ix_agent_attachments_" + column, "agent_attachments", [column])
    # SQLite ADD COLUMN 不重建已有日志表；可空归属保证所有历史日志继续可读。
    op.execute("ALTER TABLE ai_calls ADD COLUMN agent_conversation_id VARCHAR(32) REFERENCES agent_conversations(id)")
    op.create_index("ix_ai_calls_agent_conversation_id", "ai_calls", ["agent_conversation_id"])


def downgrade():
    op.drop_index("ix_ai_calls_agent_conversation_id", table_name="ai_calls")
    op.drop_column("ai_calls", "agent_conversation_id")
    op.drop_table("agent_attachments")
