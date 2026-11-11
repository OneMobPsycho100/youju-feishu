"""新增私人 Agent 会话、运行、可重放事件和待确认业务提案。"""
from alembic import op
import sqlalchemy as sa

revision = "0006_agent_conversations"
down_revision = "0005_sqlite_money"
branch_labels = None
depends_on = None

TABLES = ("agent_conversations", "agent_messages", "agent_runs", "agent_events", "agent_proposals")


def upgrade():
    # 固定本次契约，不从未来 ORM 元数据动态建表，保证旧库可按版本重放。
    op.create_table("agent_conversations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("harness_initialized", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_table("agent_messages",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("conversation_id", sa.String(32), sa.ForeignKey("agent_conversations.id"), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=True), sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False), sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_table("agent_runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("conversation_id", sa.String(32), sa.ForeignKey("agent_conversations.id"), nullable=False),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("active_owner", sa.String(64), nullable=True, unique=True),
        sa.Column("request_id", sa.String(100), nullable=False), sa.Column("user_message_id", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("stage", sa.String(150), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False), sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(600), nullable=False), sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False), sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("conversation_id", "request_id", name="uq_agent_message_request"))
    op.create_table("agent_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(32), sa.ForeignKey("agent_conversations.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False), sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_agent_event_seq"))
    op.create_table("agent_proposals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("conversation_id", sa.String(32), sa.ForeignKey("agent_conversations.id"), nullable=False),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False), sa.Column("target_id", sa.String(100), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False), sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("display", sa.JSON(), nullable=False), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("error", sa.String(600), nullable=False), sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    for table, columns in {"agent_conversations": ("owner_id",), "agent_messages": ("conversation_id", "run_id"),
            "agent_runs": ("conversation_id", "owner_id", "status"), "agent_events": ("conversation_id",),
            "agent_proposals": ("conversation_id", "run_id")}.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for name in reversed(TABLES):
        op.drop_table(name)
