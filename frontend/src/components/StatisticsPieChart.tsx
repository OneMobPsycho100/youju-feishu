import type { KeyboardEvent } from "react";
import { Empty, Tooltip } from "antd";
import type { StatisticsGroup } from "../statisticsTypes";

interface StatisticsPieChartProps {
  /** 当前筛选和统计维度下的全部分组，顺序与汇总表保持一致。 */
  groups: StatisticsGroup[];
  /** 后端精确汇总的金额文本，单位元；只将绘图尺寸转换为数值。 */
  totalAmount: string;
  /** 当前维度显示名称，例如“报销类别”或“报销人”。 */
  dimensionLabel: string;
  /** 沿用统计页面的十进制文本展示，不重新计算金额或标签占比。 */
  formatAmount: (value: string) => string;
  /** 扇区和图例共用既有的分组明细入口，传回完整真实分组。 */
  onSelect: (group: StatisticsGroup) => void;
}

const colors = ["#4f83ef", "#4fb8a0", "#a389e3", "#f1b963", "#e98b8e", "#6dc1d5", "#8a9dd5", "#a2bf72", "#d494c2", "#d2ac8c"];

function point(angle: number) {
  const radians = (angle - 90) * Math.PI / 180;
  return `${(120 + 108 * Math.cos(radians)).toFixed(6)} ${(120 + 108 * Math.sin(radians)).toFixed(6)}`;
}

/** 嵌入共用金额分布区域的饼图内容；复用分组数据，标题和图表切换由页面统一管理。 */
export function StatisticsPieChart({ groups, totalAmount, dimensionLabel, formatAmount, onSelect }: StatisticsPieChartProps) {
  const total = Number(totalAmount);
  const entries = groups.map((group, index) => ({ group, color: colors[index % colors.length] }));
  const positive = entries.filter(({ group }) => Number(group.amount) > 0);
  let angle = 0;
  const slices = positive.map((entry, index) => {
    const start = angle;
    // 只使用金额比例绘制几何，不使用已四舍五入的百分数，避免微小金额被误当作零。
    // 最后一个扇区闭合到360度，消除浮点累积缝隙；金额和占比标签仍使用后端原始文本。
    angle = index === positive.length - 1 ? 360 : Math.min(360, angle + Number(entry.group.amount) / total * 360);
    return { ...entry, path: `M 120 120 L ${point(start)} A 108 108 0 ${angle - start > 180 ? 1 : 0} 1 ${point(angle)} Z` };
  });
  return !groups.length || total <= 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={groups.length ? "报销总额为0，暂无金额占比" : "当前筛选下暂无报销申请"} /> : <div className="statistics-pie-layout">
      <div className="statistics-pie-figure">
        <svg viewBox="0 0 240 240" role="group" aria-label={`${dimensionLabel}金额占比饼图`} className="statistics-pie-chart">
          {slices.map(({ group, color, path }) => {
            const label = `${group.name}：¥${formatAmount(group.amount)}，占比${group.percentage}%，查看审批明细`;
            const interaction = {
              className: "statistics-pie-slice", fill: color, role: "button", tabIndex: 0, "aria-label": label,
              onClick: () => onSelect(group),
              onKeyDown: (event: KeyboardEvent<SVGElement>) => {
                if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(group); }
              },
            };
            return <Tooltip key={group.key} title={<><strong>{group.name}</strong><br />¥{formatAmount(group.amount)} · {group.count} 单<br />占比 {group.percentage}%</>}>
              {/* 单一非零分组必须使用完整圆形；SVG 的同起终点圆弧不能表示整圆。 */}
              {slices.length === 1 ? <circle cx={120} cy={120} r={108} {...interaction} /> : <path d={path} {...interaction} />}
            </Tooltip>;
          })}
        </svg>
      </div>
      <div className="statistics-pie-legend" aria-label={`${dimensionLabel}金额占比图例`}>
        {entries.map(({ group, color }) => <button type="button" key={group.key} className="statistics-pie-legend-row"
          title={`${group.name} · ${group.key}${group.identity_pending ? " · 身份待补齐" : ""}`} onClick={() => onSelect(group)}>
          <i style={{ backgroundColor: color }} aria-hidden="true" />
          <span className="statistics-pie-legend-name"><strong>{group.name}</strong><small>{group.count} 单{group.identity_pending ? " · 身份待补齐" : ""}</small></span>
          <span className="statistics-pie-legend-amount" title={`¥${formatAmount(group.amount)}`}>¥{formatAmount(group.amount)}</span><b>{group.percentage}%</b>
        </button>)}
      </div>
    </div>;
}
