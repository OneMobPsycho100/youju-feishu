import "@ant-design/v5-patch-for-react-19";
import React from "react";
import ReactDOM from "react-dom/client";
import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import App from "./App";
import "./styles.css";

// 组件库与日期库统一使用简体中文，确保日历月份、星期和每周起始日采用同一语言配置。
dayjs.locale("zh-cn");

const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
const getReducedMotion = () => reducedMotionQuery.matches;

/** 订阅系统动画偏好；保持订阅函数稳定，并在组件卸载时移除监听。 */
function subscribeReducedMotion(onChange: () => void) {
  reducedMotionQuery.addEventListener("change", onChange);
  return () => reducedMotionQuery.removeEventListener("change", onChange);
}

function WorkbenchRoot() {
  const reducedMotion = React.useSyncExternalStore(
    subscribeReducedMotion,
    getReducedMotion,
  );

  // 由组件库跳过动画流程，避免 CSS 禁用动画后弹层一直等待结束事件。
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          motion: !reducedMotion,
          colorPrimary: "#1665f5",
          colorSuccess: "#18a277",
          colorWarning: "#d99128",
          colorError: "#e45a59",
          colorText: "#25314b",
          colorTextSecondary: "#52627a",
          colorBorder: "#e5eaf2",
          borderRadius: 8,
          fontFamily:
            'Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
          fontSize: 14,
          fontSizeSM: 13,
          controlHeight: 36,
        },
        components: {
          Table: {
            headerBg: "#f7f9fc",
            headerColor: "#52627a",
            // 与 styles.css 的表格字号保持一致，各密度只调整留白。
            cellFontSize: 15,
            cellFontSizeMD: 15,
            cellFontSizeSM: 15,
            cellPaddingBlock: 16,
            cellPaddingBlockMD: 16,
            cellPaddingBlockSM: 14,
          },
          Button: { fontWeight: 500 },
          Menu: { itemSelectedBg: "#eaf1ff", itemSelectedColor: "#1665f5" },
          Tabs: { titleFontSize: 14 },
        },
      }}
    >
      <AntApp>
        <App />
      </AntApp>
    </ConfigProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <WorkbenchRoot />
  </React.StrictMode>,
);
