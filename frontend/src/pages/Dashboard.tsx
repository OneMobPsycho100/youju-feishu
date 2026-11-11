import { Button, Progress, Table, Tag, Tooltip } from "antd";
import {
  ArrowRightOutlined,
  AuditOutlined,
  CheckCircleFilled,
  FileDoneOutlined,
  FolderOpenOutlined,
  SafetyCertificateOutlined,
  ThunderboltFilled,
  WalletOutlined,
} from "@ant-design/icons";
import { localTime, money, query, useResource } from "../api";
import { useWorkbench } from "../context";
import type { Approval, Overview } from "../types";
import {
  ApplicantCell,
  CompanyName,
  DecisionTag,
  LinkButton,
  ResourceState,
  SectionTitle,
} from "../ui";

export function Dashboard() {
  const { companyId, tick, navigate, companies } = useWorkbench();
  const resource = useResource<Overview>(
    query("/api/v1/overview", { company_id: companyId }),
    tick,
  );
  const data = resource.data;
  const max = Math.max(1, ...(data?.trend.map((x) => Number(x.amount)) || []));
  const riskTotal = data?.risks.reduce((n, x) => n + x.count, 0) || 1;
  const stats = data
    ? [
        {
          title: "待处理预审",
          value: data.pending_count,
          suffix: "单",
          icon: <AuditOutlined />,
          color: "blue",
          detail: (
            <>
              <span className="orange-text">{data.review_count} 单需关注</span>
            </>
          ),
        },
        {
          title: "累计审批金额",
          value: money(data.approval_amount),
          suffix: "元",
          icon: <WalletOutlined />,
          color: "purple",
          detail: (
            <>
              <span>{data.approval_count} 张审批单</span>
            </>
          ),
        },
        {
          title: "统一发票台账",
          value: data.invoice_count,
          suffix: "张",
          icon: <FileDoneOutlined />,
          color: "teal",
          detail: (
            <>
              <span>含历史导入</span>
            </>
          ),
        },
        {
          title: "已归档原件",
          value: data.archived_count,
          suffix: "份",
          icon: <FolderOpenOutlined />,
          color: "amber",
          detail: (
            <>
              <span>
                {data.quarantined_count
                  ? `${data.quarantined_count} 份待确认`
                  : "无待确认原件"}
              </span>
            </>
          ),
        },
      ]
    : [];
  return (
    <ResourceState loading={resource.loading} error={resource.error}>
      <div className="stats-grid">
        {stats.map((stat) => (
          <div className="stat-card" key={stat.title}>
            <div className="stat-label">
              {stat.title}
              <span className={`stat-icon ${stat.color}`}>{stat.icon}</span>
            </div>
            <div className="stat-value">
              {stat.value}
              <small>{stat.suffix}</small>
            </div>
            <div className="stat-detail">{stat.detail}</div>
          </div>
        ))}
      </div>
      <div className="chart-grid">
        <section className="surface trend-panel">
          <SectionTitle
            title="费用申请趋势"
            help="按审批采集日期统计申请金额；预审通过不代表已付款。"
            action={<Tag className="subtle-tag">近 30 天</Tag>}
          />
          <div className="chart-subtotal">
            <span>费用申请总额</span>
            <strong>
              ¥{" "}
              {money(data?.trend.reduce((sum, x) => sum + Number(x.amount), 0))}
            </strong>
          </div>
          <div className="chart-area">
            <div className="chart-y-labels">
              {[1, 0.75, 0.5, 0.25, 0].map((x) => (
                <span key={x}>{Math.round(max * x).toLocaleString()}</span>
              ))}
            </div>
            <div className="bar-chart">
              <div className="chart-lines">
                {[0, 1, 2, 3, 4].map((x) => (
                  <i key={x} />
                ))}
              </div>
              <div className="chart-bars">
                {data?.trend.map((point, index) => (
                  <Tooltip
                    key={point.date}
                    title={
                      <>
                        {point.date}
                        <br />
                        费用 ¥{money(point.amount)} · {point.count} 单
                      </>
                    }
                  >
                    <div className="bar-column">
                      <div
                        className={`chart-bar ${index === data.trend.length - 1 ? "current" : ""} ${Number(point.amount) ? "has-value" : ""}`}
                        style={{
                          height: `${Math.max(Number(point.amount) ? 5 : 1.5, (Number(point.amount) / max) * 100)}%`,
                        }}
                      />
                      <span className="chart-x-label">
                        {index % 6 === 0 || index === data.trend.length - 1
                          ? point.date.slice(5).replace("-", "/")
                          : ""}
                      </span>
                    </div>
                  </Tooltip>
                ))}
              </div>
            </div>
          </div>
          <div className="chart-legend">
            <i />
            申请金额
          </div>
        </section>
        <section className="surface risk-panel">
          <SectionTitle
            title="预审关注事项"
          />
          <div className="risk-headline">
            <div className="risk-ring">
              <SafetyCertificateOutlined />
            </div>
            <div>
              <strong>
                {data?.review_count || 0}
                <small>单</small>
              </strong>
              <span>待复核</span>
            </div>
            <Tag color="orange">待复核</Tag>
          </div>
          <div className="risk-breakdown">
            {data?.risks.length ? (
              data.risks.slice(0, 4).map((risk, index) => (
                <div className="risk-row" key={risk.name}>
                  <div>
                    <span>
                      <i
                        style={{
                          background: [
                            "#f0a448",
                            "#ec7270",
                            "#8f8ad8",
                            "#699cec",
                          ][index],
                        }}
                      />
                      {risk.name}
                    </span>
                    <b>
                      {risk.count}
                      <small>项</small>
                    </b>
                  </div>
                  <Progress
                    percent={Math.round((risk.count / riskTotal) * 100)}
                    showInfo={false}
                    strokeColor={
                      ["#f0a448", "#ec7270", "#8f8ad8", "#699cec"][index]
                    }
                    trailColor="#f2f5f9"
                    size="small"
                  />
                </div>
              ))
            ) : (
              <div className="all-clear">
                <CheckCircleFilled />
                当前没有待复核风险
              </div>
            )}
          </div>
          <Button
            block
            className="risk-action"
            onClick={() => navigate("approvals")}
          >
            查看预审详情
            <ArrowRightOutlined />
          </Button>
        </section>
      </div>
      <div className="bottom-grid">
        <section className="surface recent-panel">
          <SectionTitle
            title="待处理审批"
            action={
              <LinkButton onClick={() => navigate("approvals")}>
                全部审批
              </LinkButton>
            }
          />
          <Table<Approval>
            rowKey="id"
            size="middle"
            pagination={false}
            tableLayout="fixed"
            scroll={{ x: 860 }}
            dataSource={data?.recent_approvals}
            columns={[
              {
                title: "报销申请",
                dataIndex: "title",
                width: 320,
                render: (_, row) => (
                  <button
                    className="table-title-link"
                    onClick={() => navigate(`approvals/${row.id}`)}
                  >
                    <strong title={row.title}>{row.title}</strong>
                    <small title={row.serial_number}>{row.serial_number}</small>
                  </button>
                ),
              },
              {
                title: "申请人",
                dataIndex: "applicant",
                width: 170,
                render: (name: string) => <ApplicantCell name={name} />,
              },
              {
                title: "报销金额",
                dataIndex: "amount",
                align: "right",
                width: 160,
                render: (value: string) => (
                  <span className="amount-text">¥{money(value)}</span>
                ),
              },
              {
                title: "预审结果",
                dataIndex: "decision",
                width: 145,
                render: (value) => <DecisionTag value={value} />,
              },
              {
                title: "",
                key: "view",
                width: 65,
                render: (_, row) => (
                  <Button
                    type="text"
                    size="small"
                    icon={<ArrowRightOutlined />}
                    aria-label={`查看${row.title}`}
                    onClick={() => navigate(`approvals/${row.id}`)}
                  />
                ),
              },
            ]}
          />
        </section>
        <section className="surface activity-panel">
          <SectionTitle
            title="最近动态"
          />
          <div className="activity-list">
            {data?.activity.slice(0, 6).map((activity, index) => (
              <div className="activity-row" key={activity.id}>
                <span className={`activity-dot activity-dot-${index % 3}`}>
                  {activity.actor.includes("Agent") ? (
                    <ThunderboltFilled />
                  ) : (
                    <FolderOpenOutlined />
                  )}
                </span>
                <div>
                  <strong>{activity.action}</strong>
                  <p>{activity.target}</p>
                  <small>
                    {activity.actor} · {localTime(activity.created_at)}
                  </small>
                </div>
              </div>
            ))}
          </div>
          {data?.failed_jobs ? (
            <Button
              type="link"
              danger
              onClick={() => navigate("settings/jobs")}
            >
              {data.failed_jobs} 个后台任务需要处理
              <ArrowRightOutlined />
            </Button>
          ) : (
            <div className="activity-healthy">
              <i />
              暂无失败的后台任务
            </div>
          )}
        </section>
      </div>
      <div className="company-strip">
        <span>
          <SafetyCertificateOutlined />
          公司主体
        </span>
        <div>
          {companies
            .filter((x) => !companyId || x.id === companyId)
            .map((x) => (
              <CompanyName key={x.id} id={x.id} compact />
            ))}
        </div>
      </div>
    </ResourceState>
  );
}
