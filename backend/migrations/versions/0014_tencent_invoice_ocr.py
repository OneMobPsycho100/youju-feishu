"""初始化默认关闭的腾讯云发票识别配置。"""
from alembic import op
import sqlalchemy as sa

revision = "0014_tencent_invoice_ocr"
down_revision = "0013_agent_attachments"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "invoice_ocr_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False, comment="Fernet 加密的 SecretId 和 SecretKey"),
        sa.Column("version", sa.Integer(), nullable=False, comment="配置乐观锁版本"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.execute(table.insert().values(id=1, enabled=False, credentials_encrypted="", version=0,
        created_at=sa.func.current_timestamp(), updated_at=sa.func.current_timestamp()))


def downgrade():
    op.drop_table("invoice_ocr_config")
