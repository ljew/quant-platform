import { useEffect, useRef } from "react";
import { BarChart, CandlestickChart, HeatmapChart, LineChart, ScatterChart } from "echarts/charts";
import {
  AriaComponent,
  AxisPointerComponent,
  DatasetComponent,
  DataZoomComponent,
  GraphicComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
  TransformComponent,
  VisualMapComponent,
} from "echarts/components";
import { init, use, type EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsOption } from "echarts";
import { useTheme } from "../theme";

// 只注册项目实际使用的图表与组件，避免完整 ECharts 进入首屏及公共包。
use([
  BarChart,
  CandlestickChart,
  HeatmapChart,
  LineChart,
  ScatterChart,
  AriaComponent,
  AxisPointerComponent,
  DatasetComponent,
  DataZoomComponent,
  GraphicComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
  TransformComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

/** ECharts 封装：传入 option 自动渲染/更新；自动注入主题背景与文字色。 */
export default function EChart({
  option,
  height = 360,
}: {
  option: EChartsOption;
  height?: number | string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<EChartsType | null>(null);
  const { colors } = useTheme();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const chart = init(el);
    chartRef.current = chart;
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);

    // 合并页签（MergedTabs）用 display:none 藏非当前页。图表在隐藏容器里 init
    // 量不到宽度，ECharts 会退回 100px 默认值；切回来时浏览器不发 window.resize，
    // 于是整张图永远卡在 100px 宽、右侧一大片空白。
    // 用 ResizeObserver 盯容器自身尺寸，可见/变宽时补一次 resize。
    let raf = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => chart.resize());
    });
    ro.observe(el);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener("resize", onResize);
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!chartRef.current) return;
    const themed: EChartsOption = {
      backgroundColor: colors.chartBg,
      textStyle: { color: colors.text },
      ...option,
      legend: option.legend ? { textStyle: { color: colors.muted }, ...(option.legend as object) } : undefined,
    };
    chartRef.current.setOption(themed, true);
    chartRef.current.resize();
  }, [option, colors]);

  return <div ref={ref} style={{ width: "100%", height }} />;
}
