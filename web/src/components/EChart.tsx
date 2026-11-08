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
    if (!ref.current) return;
    const chart = init(ref.current);
    chartRef.current = chart;
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
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
