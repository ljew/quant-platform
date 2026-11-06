/** 自动研究（数据中心 · Tab）：运行总览为主体，研究驾驶舱的 8 步流水线实况收编为子页签。 */
import { MergedTabs } from "../components/MergedTabs";
import AutoResearchPage from "./AutoResearchPage";
import ResearchPage from "./ResearchPage";

export default function AutoResearchHubPage() {
  return (
    <MergedTabs
      tabs={[
        { key: "auto", label: "运行总览", node: <AutoResearchPage /> },
        { key: "cockpit", label: "流水线实况", node: <ResearchPage /> },
      ]}
    />
  );
}
