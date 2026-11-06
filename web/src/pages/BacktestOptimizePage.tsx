/** 回测寻优（策略研究 · Tab）：单次回测与批量参数寻优收编为一页两个子页签。 */
import { MergedTabs } from "../components/MergedTabs";
import BacktestPage from "./BacktestPage";
import OptimizePage from "./OptimizePage";

export default function BacktestOptimizePage() {
  return (
    <MergedTabs
      tabs={[
        { key: "backtest", label: "单次回测", node: <BacktestPage /> },
        { key: "optimize", label: "参数寻优（批量回测）", node: <OptimizePage /> },
      ]}
    />
  );
}
