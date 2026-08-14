import { useEffect, useRef } from "react";
import * as echarts from "echarts";

/** ECharts 封装：传入 option 自动渲染/更新。 */
export default function EChart({
  option,
  height = 360,
}: {
  option: echarts.EChartsOption;
  height?: number | string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

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
    chartRef.current?.setOption(option, true);
  }, [option]);

  return <div ref={ref} style={{ width: "100%", height }} />;
}
