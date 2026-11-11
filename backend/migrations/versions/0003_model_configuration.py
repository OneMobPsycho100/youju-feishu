"""持久化模型配置、节点提示词，以及回写期间的延迟重审标记。"""
from alembic import op
import sqlalchemy as sa

revision = "0003_model_configuration"
down_revision = "0002_reconciliation"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "model_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("managed", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False, comment="兼容流式 Chat Completions 的 API 根地址"),
        sa.Column("model_name", sa.String(200), nullable=False, comment="供应商模型 ID"),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False, comment="Fernet 加密的 API Key"),
        sa.Column("default_prompt", sa.Text(), nullable=False, comment="全局业务审核要求"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    # 固定单例行使首次保存也能通过行锁与版本号串行化；不把环境密钥写入迁移。
    op.execute(table.insert().values(id=1, managed=False, enabled=False, base_url="", model_name="",
        api_key_encrypted="", default_prompt="", version=0, connection_version=0,
        created_at=sa.func.current_timestamp(), updated_at=sa.func.current_timestamp()))
    op.add_column("approval_templates", sa.Column("agent_prompt", sa.Text(), nullable=False,
        # MySQL 8 要求 TEXT 默认值使用括号表达式，SQLite 同样接受该写法。
        server_default=sa.text("('')"), comment="节点业务审核要求；空值继承全局默认"))
    op.add_column("approvals", sa.Column("audit_requested", sa.Boolean(), nullable=False,
        server_default=sa.false(), comment="配置变更后待重审，回写对账结束后补发"))
    # 存量审核没有配置指纹，升级后由启动恢复逻辑排队重审，避免旧 PASS 被直接回写。
    op.execute("UPDATE approvals SET decision = 'PENDING', audit_attachment_version = '', "
               "audit_requested = 1, version = version + 1 WHERE status = 'PENDING'")


def downgrade():
    op.drop_column("approvals", "audit_requested")
    op.drop_column("approval_templates", "agent_prompt")
    op.drop_table("model_config")
