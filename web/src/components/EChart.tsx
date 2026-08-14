import { useEffect, useRef } from "react";
import * as echarts from "echarts";
import { useTheme } from "../theme";

/** ECharts 封装：传入 option 自动渲染/更新；自动注入主题背景与文字色。 */
export default function EChart({
  option,
  height = 360,
}: {
  option: echarts.EChartsOption;
  height?: number | string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const { colors } = useTheme();

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
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
    const themed: echarts.EChartsOption = {
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
