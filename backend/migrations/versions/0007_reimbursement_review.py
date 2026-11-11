"""简化默认报销语义要求，清理旧联调提示词，并安排存量待办按新规则重审。"""
from alembic import op
import sqlalchemy as sa

revision = "0007_reimbursement_review"
down_revision = "0006_agent_conversations"
branch_labels = None
depends_on = None

# 固定迁移时的文本，避免后续修改运行时代码改变旧数据库升级结果。
OLD_DEFAULT_PROMPT = (
    "你是财务预审辅助。核对报销事由、费用类型与票面费用的语义关系，"
    "识别用途不一致、信息不足或需人工核查的风险。依据所提供的数据给出风险级别和简洁的中文说明，"
    "列出可以核实的依据；信息不足时明确说明，不编造事实。"
)
OLD_DEVELOPMENT_PROMPT = (
    "仅用于开发环境审批联调。明确标记测试单据或测试票据的资料不具备真实报销、付款或税务凭证效力；"
    "需保留人工终审。核对报销事由、费用类型、主体、金额、附件及重复报销风险，信息不足时明确交由人工复核。"
)
DEFAULT_PROMPT = (
    "你是财务预审辅助，按日常报销的合理性进行简要核对。重点判断报销事由、费用类型与票面费用是否存在明确矛盾；"
    "主体、金额和重复报销由程序规则核验。事由简短但用途基本合理、没有具体异常时，风险级别应为 low。"
    "不要仅因缺少乘机人、航段、行程单、人员名单、详细业务背景或额外证明材料而提高风险级别或要求补填；"
    "销售方名称或电子发票形式本身也不构成异常。信息缺失可以简短提示，不得推定违规。"
    "只有已提供的数据能支持用途明显不一致或其他具体疑点时才给 medium 或 high，并指出具体矛盾。"
    "给出简洁的中文结论和可核实的依据，不编造事实。"
)


def upgrade():
    connection = op.get_bind()
    config = sa.table("model_config", sa.column("id", sa.Integer), sa.column("default_prompt", sa.Text),
                      sa.column("version", sa.Integer))
    templates = sa.table("approval_templates", sa.column("agent_prompt", sa.Text))
    # 只替换明确识别的旧内置文本；管理员自定义的其他业务提示词保留。
    connection.execute(config.update().where(sa.func.trim(config.c.default_prompt) == OLD_DEFAULT_PROMPT)
                       .values(default_prompt=DEFAULT_PROMPT, version=config.c.version + 1))
    connection.execute(templates.update().where(sa.func.trim(templates.c.agent_prompt) == OLD_DEVELOPMENT_PROMPT)
                       .values(agent_prompt=""))
    # 指纹和复核快照自此版本生效。启动恢复会重新排队，回写中的操作仍先对账。
    op.execute("UPDATE approvals SET decision = 'PENDING', audit_attachment_version = '', "
               "audit_requested = 1, version = version + 1 WHERE status = 'PENDING'")


def downgrade():
    # 人工意见和提示词属于业务数据，降级不删除记录，也不恢复开发环境话术。
    pass
