import { useId, useState, type ReactNode } from "react";
import { Alert, Avatar, Badge, Button, Empty, Popover, Skeleton, Tag, Tooltip } from "antd";
import {
  ArrowRightOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
  ExclamationCircleFilled,
  InfoCircleOutlined,
  LoadingOutlined,
} from "@ant-design/icons";
import { useWorkbench } from "./context";
import type { AIApprovalStatus, Approval, Decision } from "./types";

interface ApplicantCellProps {
  /** 申请人姓名；飞书尚未解析姓名时可能是较长的 open_id。 */
  name: string;
  /** 传入时显示部门行；空字符串显示缺省符号，不传则隐藏该行。 */
  department?: string;
}

/** 审批列表与总览共用，限制长身份信息的宽度并提供完整内容提示。 */
export function ApplicantCell({ name, department }: ApplicantCellProps) {
  return (
    <div className="person-cell">
      <Avatar size={32} className="person-avatar">
        {name.slice(-2)}
      </Avatar>
      <div className="person-details">
        <Tooltip title={name}>
          <span className="person-name">{name}</span>
        </Tooltip>
        {department !== undefined && (
          <Tooltip title={department || undefined}>
            <small>{department || "—"}</small>
          </Tooltip>
        )}
      </div>
    </div>
  );
}

/** previous 表示正在重审或本轮尚未完成，保留的结论属于上一次审核。 */
export function DecisionTag({ value, previous = false }: { value: Decision; previous?: boolean }) {
  const options = {
    PASS: ["通过", "success", <CheckCircleFilled key="icon" />],
    REVIEW: ["需复核", "warning", <ExclamationCircleFilled key="icon" />],
    BLOCK: ["规则阻断", "error", <ExclamationCircleFilled key="icon" />],
    PENDING: ["待预审", "default", <ClockCircleOutlined key="icon" />],
  } as const;
  const [label, color, icon] = options[value] || options.PENDING;
  const tag = (
    <Tag className="state-tag" color={color} icon={icon}>
      {label}
    </Tag>
  );
  return previous && value !== "PENDING" ? (
    <Tooltip title="这是上次预审的结论，本轮完成后会自动更新">
      <span className="previous-audit-result"><small>上次结果</small>{tag}</span>
    </Tooltip>
  ) : tag;
}

/** 展示整轮预审进度，处理中加载动画持续可见，不与飞书审批通过状态混用。 */
export function AIApprovalStatusTag({ value, enabled }: { value: AIApprovalStatus; enabled: boolean }) {
  if (!enabled) {
    return <Tooltip title="此模板由财务整单人工预审">
      <Tag className="state-tag">AI 审批已关闭</Tag>
    </Tooltip>;
  }
  const options = {
    NOT_STARTED: ["未开始", "default", <ClockCircleOutlined key="icon" />, "尚未开始本轮预审"],
    QUEUED: ["排队中", "warning", <ClockCircleOutlined key="icon" />, "等待预审执行，重试或前序处理完成后会自动继续"],
    RUNNING: ["审批中", "processing", <LoadingOutlined key="icon" spin />, "正在处理附件并执行 AI 预审，完成后自动更新"],
    COMPLETED: ["已完成", "success", <CheckCircleFilled key="icon" />, "本轮预审结论已保存，是否通过请查看预审结果"],
    FAILED: ["审批失败", "error", <ExclamationCircleFilled key="icon" />, "本轮预审执行失败，可在审批详情中重新预审"],
  } as const;
  const [label, color, icon, description] = options[value] || options.NOT_STARTED;
  return <Tooltip title={description}>
    <Tag className="state-tag" color={color} icon={icon} aria-label={`AI审批状态：${label}`}>{label}</Tag>
  </Tooltip>;
}

export function StatusTag({ value }: { value: string }) {
  const labels: Record<string, string> = {
    PENDING: "审批中",
    MISSING: "无可处理的预审待办",
    AMBIGUOUS: "多个预审待办，需人工处理",
    APPROVED: "已通过",
    REJECTED: "已拒绝",
    CANCELED: "已撤回",
    REVERTED: "已撤销",
    DELETED: "已删除",
    ARCHIVED: "已归档",
    NONE: "待归档",
    PARTIAL: "部分待确认",
    QUARANTINED: "待确认",
    REVOKED: "已撤销留痕",
    SUPERSEDED: "历史版本",
    RESERVED: "审核占用",
    USED: "审批已用",
    FROZEN: "冻结核查",
    RELEASED: "已释放",
    HISTORICAL: "历史已用",
    SUCCEEDED: "已完成",
    FAILED: "失败",
    RUNNING: "执行中",
    REQUESTED: "待处理",
    EXECUTING: "回写中",
    UNKNOWN: "结果核对中",
    COMPLETED: "已完成",
    UPLOADED: "待映射",
    PREVIEWED: "已预览",
    IMPORTING: "导入中",
  };
  const good = ["APPROVED", "ARCHIVED", "SUCCEEDED", "COMPLETED"];
  const bad = ["FAILED", "REJECTED", "FROZEN", "REVOKED"];
  return (
    <Badge
      color={
        good.includes(value)
          ? "#20a67a"
          : bad.includes(value)
            ? "#ed615d"
            : "#8f9bad"
      }
      text={<span className="status-label">{labels[value] || value}</span>}
    />
  );
}

export function CompanyName({
  id,
  compact = false,
}: {
  id?: string | null;
  compact?: boolean;
}) {
  const { companies } = useWorkbench();
  const index = companies.findIndex((x) => x.id === id);
  const company = companies[index];
  return (
    <span
      className={`company-label company-${Math.max(index, 0) % 3}`}
      title={company?.name || "待确认主体"}
    >
      <i />
      <span className="company-name">
        {company
          ? compact
            ? company.name.replace("有限公司", "")
            : company.name
          : "待确认主体"}
      </span>
    </span>
  );
}

interface ApprovalCompaniesProps {
  /** 同时包含已匹配主体、申报名称和未匹配票面名称的审批展示数据。 */
  approval: Pick<Approval, "company_ids" | "declared_company_names" | "unmatched_company_names">;
  /** 表格中省略“有限公司”后缀，完整名称始终可悬停查看。 */
  compact?: boolean;
}

/** 主体未归类时仍展示真实名称，并明确标注表单与票面来源。 */
export function ApprovalCompanies({ approval, compact = false }: ApprovalCompaniesProps) {
  const { companies } = useWorkbench();
  const matchedNames = new Set(companies
    .filter((company) => approval.company_ids.includes(company.id))
    .flatMap((company) => [company.name, ...company.aliases]));
  const declaredNames = (approval.declared_company_names || []).filter((name) => !matchedNames.has(name));
  const unmatchedNames = approval.unmatched_company_names || [];
  if (!approval.company_ids.length && !declaredNames.length && !unmatchedNames.length)
    return <span className="muted">待确认主体</span>;
  return (
    <div className="company-stack">
      {approval.company_ids.map((id) => <CompanyName key={id} id={id} compact={compact} />)}
      {declaredNames.map((name) => (
        <div className="company-candidate" key={`declared-${name}`}>
          <Tooltip title={`表单申报主体：${name}`}>
            <span className="company-candidate-name">{name}</span>
          </Tooltip>
          <small>申报</small>
        </div>
      ))}
      {unmatchedNames.map((name) => (
        <div className="company-candidate" key={`invoice-${name}`}>
          <Tooltip title={`票面购买方：${name}；尚未匹配已配置主体，需核对确认`}>
            <span className="company-candidate-name">{name}</span>
          </Tooltip>
          <small className="company-pending">票面待确认</small>
        </div>
      ))}
    </div>
  );
}

interface HelpTipProps {
  /** 帮助按钮的中文名称，同时作为弹出说明的标题。 */
  label: string;
  /** 仅在用户点击帮助入口后显示的字段说明或统计口径。 */
  children: ReactNode;
}

/** 使用原生按钮触发帮助，支持触屏、Enter/空格打开和 Escape 收起。 */
export function HelpTip({ label, children }: HelpTipProps) {
  const [open, setOpen] = useState(false);
  const contentId = useId();
  return (
    <Popover title={label} content={<div id={contentId} className="help-tip-content">{children}</div>}
      trigger="click" open={open} onOpenChange={setOpen}>
      <Button type="text" size="small" className="help-tip" icon={<InfoCircleOutlined />}
        aria-label={label} aria-expanded={open} aria-controls={open ? contentId : undefined}
        onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setOpen(false); } }} />
    </Popover>
  );
}

interface SectionTitleProps {
  /** 栏目的主要名称。 */
  title: string;
  /** 可选的实际数量、时间范围等副信息，不用于重复介绍栏目。 */
  caption?: string;
  /** 可选的统计口径或使用帮助，默认收起在标题旁。 */
  help?: ReactNode;
  /** 标题右侧的筛选或操作控件。 */
  action?: ReactNode;
}

export function SectionTitle({
  title,
  caption,
  help,
  action,
}: SectionTitleProps) {
  return (
    <div className="section-title">
      <div>
        <div className="section-title-heading"><h3>{title}</h3>{help && <HelpTip label={`${title}说明`}>{help}</HelpTip>}</div>
        {caption && <p>{caption}</p>}
      </div>
      {action}
    </div>
  );
}

export function ResourceState({
  loading,
  error,
  empty,
  children,
}: {
  loading?: boolean;
  error?: string;
  empty?: boolean;
  children: ReactNode;
}) {
  if (error)
    return (
      <Alert type="error" showIcon message="暂时无法加载" description={error} />
    );
  if (loading)
    return (
      <div className="surface loading-surface">
        <Skeleton active paragraph={{ rows: 8 }} />
      </div>
    );
  if (empty)
    return (
      <div className="surface">
        <Empty description="当前筛选下暂无业务记录" />
      </div>
    );
  return <>{children}</>;
}

export function LinkButton({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <Button type="link" onClick={onClick} className="more-link">
      {children}
      <ArrowRightOutlined />
    </Button>
  );
}
