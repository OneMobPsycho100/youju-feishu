"""持久化整轮 AI 预审进度，保留历史结论和在途任务，不触发真实重审。"""
from alembic import op
import sqlalchemy as sa


revision = "0015_approval_ai_status"
down_revision = "0014_tencent_invoice_ocr"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approvals", sa.Column("ai_status", sa.String(16), nullable=False,
        server_default="NOT_STARTED", comment="整轮预审进度，与审核结论及飞书状态独立"))
    op.add_column("approvals", sa.Column("ai_task_id", sa.String(32), nullable=True,
        comment="当前预审所属任务或直接执行标识"))
    op.add_column("approvals", sa.Column("ai_requested_at", sa.DateTime(), nullable=True,
        comment="最新预审请求时间，UTC；区分延迟旧任务与复用旧任务 ID 的显式重试"))
    op.create_index("ix_approvals_ai_task_id", "approvals", ["ai_task_id"])
    # 只使用已保存的业务快照回填，不能把预审通过等同于飞书最终通过。
    op.execute(sa.text("""
        UPDATE approvals SET
            ai_requested_at = (SELECT a.created_at FROM audit_runs a WHERE a.id = approvals.last_audit_id),
            ai_status = CASE
            WHEN audit_requested = 1 THEN 'QUEUED'
            WHEN decision <> 'PENDING' AND audit_attachment_version = attachment_version
                 AND EXISTS (SELECT 1 FROM audit_runs WHERE audit_runs.id = approvals.last_audit_id)
                THEN 'COMPLETED'
            ELSE 'NOT_STARTED' END
    """))
    # EVENT 与 ARCHIVE 会在任务内部直接调用预审。按最新请求绑定状态；归档已完成预审后才失败时保留完成状态。
    op.execute(sa.text("""
        WITH targets AS (
            SELECT j.id, j.status, j.created_at,
                CASE WHEN j.kind = 'EVENT' THEN json_extract(e.payload, '$.event.instance_code')
                     ELSE json_extract(j.payload, '$.instance_code') END AS approval_id
            FROM jobs j LEFT JOIN event_inbox e
                ON j.kind = 'EVENT' AND e.id = json_extract(j.payload, '$.event_id')
            WHERE j.kind IN ('AUDIT', 'ARCHIVE', 'EVENT')
                AND (j.kind <> 'EVENT' OR (e.status <> 'IGNORED' AND EXISTS (
                    SELECT 1 FROM approval_templates t
                    WHERE t.code = json_extract(e.payload, '$.event.approval_code') AND t.active = 1
                )))
                AND (j.kind <> 'EVENT' OR COALESCE(json_extract(e.payload, '$.event.status'), '')
                     NOT IN ('REJECTED', 'CANCELED', 'DELETED', 'REVERTED'))
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY approval_id ORDER BY created_at DESC, id DESC) AS position
            FROM targets WHERE approval_id IS NOT NULL
        )
        UPDATE approvals SET
            ai_task_id = r.id,
            ai_requested_at = CASE WHEN approvals.ai_requested_at > r.created_at
                THEN approvals.ai_requested_at ELSE r.created_at END,
            ai_status = CASE
                WHEN r.status = 'PENDING' THEN 'QUEUED'
                WHEN r.status = 'RUNNING' THEN 'RUNNING'
                WHEN r.status = 'FAILED' AND NOT EXISTS (
                    SELECT 1 FROM audit_runs a WHERE a.id = approvals.last_audit_id AND a.created_at >= r.created_at
                ) THEN 'FAILED'
                ELSE approvals.ai_status END
        FROM ranked r WHERE r.approval_id = approvals.id AND r.position = 1
    """))


def downgrade():
    op.drop_index("ix_approvals_ai_task_id", table_name="approvals")
    op.drop_column("approvals", "ai_requested_at")
    op.drop_column("approvals", "ai_task_id")
    op.drop_column("approvals", "ai_status")
