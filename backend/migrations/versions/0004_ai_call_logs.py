"""保存可追溯的 AI 调用、供应商可见思考、原始输出及阶段事件。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT

revision = "0004_ai_call_logs"
down_revision = "0003_model_configuration"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("ai_calls",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("source", sa.String(24), nullable=False, comment="AUDIT / CONNECTION_TEST"),
        sa.Column("approval_id", sa.String(64), nullable=True, comment="关联审批；连接测试为空"),
        sa.Column("template_code", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("engine", sa.String(80), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, comment="RUNNING / COMPLETED / FAILED / SKIPPED"),
        sa.Column("stage", sa.String(120), nullable=False),
        sa.Column("prompt", sa.Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False, comment="实际系统提示词，包含固定约束；已脱敏"),
        sa.Column("input_json", sa.Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False, comment="结构化输入 JSON 文本；已脱敏"),
        sa.Column("reasoning", sa.Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False, comment="仅保存供应商明确返回的思考文本"),
        sa.Column("raw_output", sa.Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False, comment="JSON 解析前的最终输出，失败时也保留已收到内容"),
        sa.Column("parsed_result", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False, comment="供应商返回的 Token 用量；缺失不估算"),
        sa.Column("events", sa.JSON(), nullable=False, comment="白名单阶段事件，不保存完整 SDK 通知或鉴权信息"),
        sa.Column("finish_reason", sa.String(100), nullable=False),
        sa.Column("error_type", sa.String(100), nullable=False),
        sa.Column("error_message", sa.String(500), nullable=False),
        sa.Column("has_reasoning", sa.Boolean(), nullable=False),
        sa.Column("has_output", sa.Boolean(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False, comment="日志超出长度上限时明确标记截断"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
    )
    for column in ("source", "approval_id", "status", "started_at"):
        op.create_index(f"ix_ai_calls_{column}", "ai_calls", [column])


def downgrade():
    op.drop_table("ai_calls")
