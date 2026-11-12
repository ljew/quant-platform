/** 回测寻优（策略研究 · Tab）：单次回测与批量参数寻优收编为一页两个子页签。 */
import { MergedTabs } from "../components/MergedTabs";
import BacktestPage from "./BacktestPage";
import OptimizePage from "./OptimizePage";

export default function BacktestOptimizePage({ inner }: { inner?: string } = {}) {
  const defaultKey = inner === "optimize" ? "optimize" : "backtest";
  const forceKey = inner === "workset-accept" ? "backtest" : undefined;
  return (
    <MergedTabs
      defaultKey={defaultKey}
      forceKey={forceKey}
      tabs={[
        { key: "backtest", label: "单次回测", node: <BacktestPage inner={inner} /> },
        { key: "optimize", label: "参数寻优（批量回测）", node: <OptimizePage /> },
      ]}
    />
  );
}
