import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import {
  App as AntApp,
  Avatar,
  Button,
  Dropdown,
  Select,
  Spin,
  Tag,
} from "antd";
import {
  ApartmentOutlined,
  AuditOutlined,
  BarChartOutlined,
  BellOutlined,
  CloudSyncOutlined,
  DashboardOutlined,
  FileProtectOutlined,
  FolderOpenOutlined,
  LogoutOutlined,
  SettingOutlined,
  ThunderboltFilled,
  RobotOutlined,
} from "@ant-design/icons";
import { api, post, waitJob } from "./api";
import type { Company, Job, User } from "./types";
import { WorkbenchContext } from "./context";
import { Dashboard } from "./pages/Dashboard";
import type { AgentPageContext } from "./agentTypes";
import type { SetupStatus } from "./feishuTypes";
const FeishuSetup = lazy(() => import("./pages/FeishuSetup").then(module => ({ default: module.FeishuSetup })));
const AgentPanel = lazy(() => import("./components/AgentPanel").then((module) => ({ default: module.AgentPanel })));
// 按工作模块加载表单与弹窗，减少财务总览首次进入时的脚本体积。
const Approvals = lazy(() =>
  import("./pages/Approvals").then((module) => ({ default: module.Approvals })),
);
const Invoices = lazy(() =>
  import("./pages/Invoices").then((module) => ({ default: module.Invoices })),
);
const Archives = lazy(() =>
  import("./pages/Archives").then((module) => ({ default: module.Archives })),
);
const Settings = lazy(() =>
  import("./pages/Settings").then((module) => ({ default: module.Settings })),
);
const Statistics = lazy(() => import("./pages/Statistics").then((module) => ({ default: module.Statistics })));

const navItems = [
  { key: "overview", label: "财务总览", icon: <DashboardOutlined /> },
  { key: "statistics", label: "报销统计", icon: <BarChartOutlined /> },
  { key: "approvals", label: "审批中心", icon: <AuditOutlined /> },
  { key: "invoices", label: "发票台账", icon: <FileProtectOutlined /> },
  { key: "archives", label: "归档中心", icon: <FolderOpenOutlined /> },
  { key: "settings", label: "系统管理", icon: <SettingOutlined /> },
];
const navigate = (path: string) => {
  window.location.hash = "/" + path;
};

export default function App() {
  const { message } = AntApp.useApp();
  const [mode, setMode] = useState("");
  const [setup, setSetup] = useState<SetupStatus | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [loginBusy, setLoginBusy] = useState(false);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [tick, setTick] = useState(0);
  const [companyId, setCompanyId] = useState("");
  const [agentOpen, setAgentOpen] = useState(false);
  const [agentBusy, setAgentBusy] = useState(false);
  const [agentPageContext, setAgentPageContext] = useState<AgentPageContext>({ selection_type: "", selected_ids: [], filters: {} });
  const openAgent = useCallback(() => setAgentOpen(true), []);
  const [route, setRoute] = useState(
    window.location.hash.slice(2) || "overview",
  );
  const refresh = useCallback(() => setTick((value) => value + 1), []);
  useEffect(() => {
    const handler = () => setRoute(window.location.hash.slice(2) || "overview");
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  useEffect(() => {
    void Promise.all([api<{ mode: string }>("/api/meta"), api<User | null>("/api/auth/session"), api<SetupStatus>("/api/setup/status")])
      .then(([meta, session, status]) => { setMode(meta.mode); setUser(session); setSetup(status); })
      .catch(() => message.error("服务尚未就绪，请检查后端连接"))
      .finally(() => setInitializing(false));
    const handler = () => setUser(null);
    window.addEventListener("session-expired", handler);
    return () => window.removeEventListener("session-expired", handler);
  }, [message]);
  useEffect(() => {
    if (user)
      void api<Company[]>("/api/v1/companies")
        .then(setCompanies)
        .catch(() => {});
  }, [user?.id, tick]);
  useEffect(() => {
    if (user)
      void api<User>("/api/auth/me")
        .then(setUser)
        .catch(() => {});
  }, [user?.id, tick]);
  useEffect(() => {
    if (!user) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) refresh();
    }, 15000);
    return () => clearInterval(timer);
  }, [user, refresh]);
  const onJob = useCallback(
    async (job: Job, label: string) => {
      // 入队后立即读取进度，让列表和详情先显示排队中，无需等到整个任务完成。
      refresh();
      void message.loading({
        key: job.id,
        content: `${label}，后台正在处理…`,
        duration: 0,
      });
      try {
        await waitJob(job);
        message.success({ key: job.id, content: `${label}已完成` });
      } catch (reason) {
        message.destroy(job.id);
        throw reason;
      } finally {
        refresh();
      }
    },
    [message, refresh],
  );
  const login = async (role: string) => {
    setLoginBusy(true);
    try {
      setUser(await post<User>(`/api/auth/demo?role=${role}`));
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setLoginBusy(false);
    }
  };
  const page = route.split("/")[0];
  const baseItem = navItems.find((x) => x.key === page) || navItems[0];
  const selected =
    user?.role === "finance" && baseItem.key === "settings"
      ? { ...baseItem, label: "后台任务" }
      : baseItem;
  if (initializing)
    return (
      <div className="full-loading">
        <Spin size="large" />
        <p>正在连接财务工作台</p>
      </div>
    );
  if (mode === "live" && (setup?.setup_required || page === "setup"))
    return <Suspense fallback={<div className="full-loading"><Spin /><p>正在打开飞书接入向导</p></div>}><FeishuSetup /></Suspense>;
  if (!user)
    return (
      <div className="login-page">
        <main className="login-main">
          <div className="brand login-brand">
            <span className="brand-mark">
              <FileProtectOutlined />
            </span>
            <strong>有据</strong>
          </div>
          <div className="login-card-wrap">
            <div className="login-card">
              <h1>登录有据</h1>
              {mode === "demo" ? (
                <>
                  <Tag className="login-demo-tag" color="blue">演示环境</Tag>
                  <Button
                    block
                    type="primary"
                    size="large"
                    loading={loginBusy}
                    onClick={() => void login("admin")}
                  >
                    管理员登录
                  </Button>
                  <div className="demo-role-options">
                    <Button
                      onClick={() => void login("finance")}
                      disabled={loginBusy}
                    >
                      财务登录
                    </Button>
                    <Button
                      onClick={() => void login("viewer")}
                      disabled={loginBusy}
                    >
                      只读登录
                    </Button>
                  </div>
                </>
              ) : (
                <Button block type="primary" size="large" href="/api/auth/feishu">
                  飞书登录
                </Button>
              )}
            </div>
            {mode !== "demo" && <Button className="login-recovery" type="link" onClick={() => navigate("setup")}>接入恢复</Button>}
          </div>
        </main>
        <aside className="login-art" aria-hidden="true">
          <svg viewBox="0 0 420 440" fill="none" focusable="false">
            <ellipse cx="213" cy="374" rx="158" ry="17" fill="#dce5f3" opacity=".5" />
            <circle cx="222" cy="212" r="145" fill="#e8edfc" />
            <rect x="68" y="125" width="175" height="223" rx="15" transform="rotate(-12 68 125)" fill="#e2eaff" stroke="#cfdbf5" />
            <path d="M119 80h127l57 57v196a14 14 0 0 1-14 14H119a14 14 0 0 1-14-14V94a14 14 0 0 1 14-14Z" fill="white" stroke="#d8e2f2" strokeWidth="2" />
            <path d="M246 81v42a14 14 0 0 0 14 14h42" fill="#f0f4fd" />
            <rect x="132" y="121" width="54" height="9" rx="4.5" fill="#81a3ed" />
            <path d="M132 156h97M132 174h68" stroke="#e0e7f4" strokeWidth="7" strokeLinecap="round" />
            <rect x="136" y="255" width="29" height="59" rx="5" fill="#d6e3ff" />
            <rect x="180" y="228" width="29" height="86" rx="5" fill="#a9c2fc" />
            <rect x="224" y="199" width="29" height="115" rx="5" fill="#78a2f4" />
            <path d="M128 315h134" stroke="#e4eaf5" strokeWidth="2" strokeLinecap="round" />
            <circle cx="294" cy="319" r="43" fill="#e0eee9" />
            <circle cx="294" cy="319" r="31" fill="#76b79f" />
            <path d="m280 319 9 9 18-19" stroke="white" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx="72" cy="259" r="12" fill="#a7bdf0" />
            <circle cx="328" cy="166" r="7" fill="#c3d4f7" />
            <path d="M307 83h18M316 74v18" stroke="#b4c6ed" strokeWidth="3" strokeLinecap="round" />
          </svg>
        </aside>
      </div>
    );
  return (
    <WorkbenchContext.Provider
      value={{
        user,
        mode,
        companies,
        tick,
        companyId,
        setCompanyId,
        refresh,
        onJob,
        navigate,
        setAgentPageContext,
        openAgent,
      }}
    >
      <div className="app-shell">
        <aside className="sidebar">
          <a className="brand" href="#/overview">
            <span className="brand-mark">
              <FileProtectOutlined />
            </span>
            <div>
              <strong>
                有据
              </strong>
              <small>财务工作台</small>
            </div>
          </a>
          <div className="workspace-label">
            <span className="workspace-icon">
              <ApartmentOutlined />
            </span>
            <div>
              <b>企业财务空间</b>
              <small>{companies.length} 个公司主体</small>
            </div>
          </div>
          <nav>
            {navItems
              .filter((x) => x.key !== "settings" || user.role !== "viewer")
              .map((item) => (
                <a
                  key={item.key}
                  href={`#/${item.key === "settings" && user.role === "finance" ? "settings/jobs" : item.key}`}
                  className={`nav-item ${selected.key === item.key ? "active" : ""}`}
                >
                  <span>{item.icon}</span>
                  {item.key === "settings" && user.role === "finance"
                    ? "后台任务"
                    : item.label}
                  {item.key === "approvals" && <i className="nav-dot" />}
                </a>
              ))}
          </nav>
          <div className="sidebar-bottom">
            <button className="agent-card agent-launch" onClick={openAgent} aria-label="打开业务助手">
              <span className="agent-card-icon">
                <ThunderboltFilled />
              </span>
              <div>
                <b>业务助手</b>
                {agentBusy && <span>
                  <i />
                  处理中
                </span>}
              </div>
              <span className="agent-card-glow" />
            </button>
          </div>
        </aside>
        <div className="main-shell">
          <header className="topbar">
            <div className="breadcrumb">
              工作空间<span>/</span>
              <b>{selected.label}</b>
            </div>
            <div className="topbar-right">
              <Button type="text" icon={<RobotOutlined />} onClick={openAgent} className="agent-topbar-entry">
                {agentBusy ? "业务助手处理中" : "业务助手"}
              </Button>
              {mode === "demo" && <Tag className="demo-tag">演示环境</Tag>}
              <span className="topbar-date">
                {new Intl.DateTimeFormat("zh-CN", {
                  timeZone: "Asia/Shanghai",
                  year: "numeric",
                  month: "2-digit",
                  day: "2-digit",
                }).format(new Date())}
              </span>
              <Button
                type="text"
                icon={<BellOutlined />}
                aria-label="查看异常任务"
                onClick={() =>
                  user.role !== "viewer"
                    ? navigate("settings/jobs")
                    : message.info("异常任务可由管理员在系统管理中查看")
                }
              />
              <i className="topbar-divider" />
              <Dropdown
                menu={{
                  items: [
                    {
                      key: "role",
                      label: `${{ admin: "管理员", finance: "财务人员", viewer: "只读用户" }[user.role]} · ${user.name}`,
                      disabled: true,
                    },
                    { type: "divider" },
                    {
                      key: "logout",
                      label: "退出登录",
                      icon: <LogoutOutlined />,
                      onClick: () =>
                        void post("/api/auth/logout").then(() => setUser(null)),
                    },
                  ],
                }}
              >
                <button className="user-menu">
                  <Avatar
                    size={32}
                    style={{ background: "#e9effc", color: "#3152a3" }}
                  >
                    {user.name.slice(-2)}
                  </Avatar>
                  <span>{user.name}</span>
                  <small>⌄</small>
                </button>
              </Dropdown>
            </div>
          </header>
          <main className="page-content">
            <div className="page-heading">
              <h1>{selected.label}</h1>
              <div className="page-heading-actions">
                {page === "overview" && <Button type="primary" onClick={() => navigate("approvals")}>处理审批</Button>}
                <Select
                  value={companyId}
                  onChange={setCompanyId}
                  className="global-company-select"
                  suffixIcon={<ApartmentOutlined />}
                  options={[
                    { value: "", label: "全部公司主体" },
                    ...companies.map((x) => ({ value: x.id, label: x.name })),
                  ]}
                />
                <Button icon={<CloudSyncOutlined />} onClick={refresh}>
                  刷新数据
                </Button>
              </div>
            </div>
            <Suspense
              fallback={
                <div
                  className="surface full-loading"
                  style={{ minHeight: 400 }}
                >
                  <Spin />
                  <span>正在打开工作模块</span>
                </div>
              }
            >
              {page === "statistics" ? (
                <Statistics />
              ) : page === "approvals" ? (
                <Approvals detailId={route.split("/")[1]} />
              ) : page === "invoices" ? (
                <Invoices />
              ) : page === "archives" ? (
                <Archives />
              ) : page === "settings" ? (
                user.role !== "viewer" ? (
                  <Settings section={route.split("/")[1]} />
                ) : (
                  <div className="surface">需要管理员权限</div>
                )
              ) : (
                <Dashboard />
              )}
            </Suspense>
          </main>
        </div>
      </div>
      <Suspense fallback={null}>
        <AgentPanel key={user.id} open={agentOpen} onClose={() => setAgentOpen(false)} onBusyChange={setAgentBusy}
          context={{ route, company_id: companyId, approval_id: page === "approvals" ? route.split("/")[1] || "" : "", ...agentPageContext }} />
      </Suspense>
    </WorkbenchContext.Provider>
  );
}
