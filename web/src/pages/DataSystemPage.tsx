/** 数据与系统（数据中心 · Tab）：数据管道为主体，系统监控（服务/磁盘/数据流）收编为子页签。 */
import { MergedTabs } from "../components/MergedTabs";
import DataPipelinePage from "./DataPipelinePage";
import MonitorPage from "./MonitorPage";

export default function DataSystemPage() {
  return (
    <MergedTabs
      tabs={[
        { key: "pipeline", label: "数据管道", node: <DataPipelinePage /> },
        { key: "monitor", label: "系统服务", node: <MonitorPage /> },
      ]}
    />
  );
}
