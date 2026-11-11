"""飞书接入页面配置、初始化授权摘要和接收服务状态。"""
from alembic import op
import sqlalchemy as sa

revision = "0012_feishu_web_configuration"
down_revision = "0011_sync_configuration"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table("feishu_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("managed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("app_id", sa.String(100), nullable=False),
        sa.Column("app_secret_encrypted", sa.Text(), nullable=False),
        sa.Column("tenant_key", sa.String(100), nullable=False),
        sa.Column("public_url", sa.String(2048), nullable=False),
        sa.Column("event_transport", sa.String(16), nullable=False),
        sa.Column("encrypt_key_encrypted", sa.Text(), nullable=False),
        sa.Column("verification_token_encrypted", sa.Text(), nullable=False),
        sa.Column("initial_admin_ids", sa.JSON(), nullable=False),
        sa.Column("imported_from_env", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("credential_verified_at", sa.DateTime(), nullable=True),
        sa.Column("receiver_status", sa.String(24), nullable=False),
        sa.Column("receiver_applied_version", sa.Integer(), nullable=False),
        sa.Column("receiver_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("receiver_error", sa.String(500), nullable=False),
        sa.Column("last_event_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    access = op.create_table("setup_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("generation", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("code_expires_at", sa.DateTime(), nullable=True),
        sa.Column("draft_encrypted", sa.Text(), nullable=False),
        sa.Column("draft_owner", sa.String(64), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("draft_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.execute(table.insert().values(id=1, managed=False, status="UNCONFIGURED", app_id="", app_secret_encrypted="",
        tenant_key="", public_url="", event_transport="websocket", encrypt_key_encrypted="", verification_token_encrypted="",
        initial_admin_ids=[], imported_from_env=False, version=0, connection_version=0, receiver_status="WAITING",
        receiver_applied_version=0, receiver_error="", created_at=sa.func.current_timestamp(), updated_at=sa.func.current_timestamp()))
    op.execute(access.insert().values(id=1, code_hash="", generation="", purpose="INIT", draft_encrypted="",
        draft_owner="", draft_version=0, created_at=sa.func.current_timestamp(), updated_at=sa.func.current_timestamp()))


def downgrade():
    op.drop_table("setup_access")
    op.drop_table("feishu_config")
