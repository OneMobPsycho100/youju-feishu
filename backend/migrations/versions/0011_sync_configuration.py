"""持久化自动增量补偿同步开关、执行间隔及调度计时。"""
from alembic import op
import sqlalchemy as sa

revision = "0011_sync_configuration"
down_revision = "0010_payment_workflow"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "sync_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("managed", sa.Boolean(), nullable=False, comment="首次在工作台保存后使用数据库配置"),
        sa.Column("enabled", sa.Boolean(), nullable=False, comment="是否启用自动增量补偿同步"),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, comment="同步间隔，单位秒"),
        sa.Column("version", sa.Integer(), nullable=False, comment="配置乐观锁版本"),
        sa.Column("schedule_anchor_at", sa.DateTime(), nullable=True, comment="自动调度计时起点，UTC"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    # 默认继续读取部署环境；迁移不重置已有审批游标或创建真实飞书任务。
    op.execute(table.insert().values(id=1, managed=False, enabled=True, interval_seconds=300, version=0,
        schedule_anchor_at=None, created_at=sa.func.current_timestamp(), updated_at=sa.func.current_timestamp()))


def downgrade():
    op.drop_table("sync_config")
