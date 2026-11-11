/**
 * 基金研究（策略研究 · Tab）：持续性 + 持仓穿透收编为一个入口、两个子页签。
 *
 * 为什么合并：这两个页面回答的是同一个问题的两半 ——
 * 「这只基金的业绩能不能延续」（持续性）和「它的超额到底买了什么」（穿透）。
 * 原来是两个平级 tab，看一只基金要来回跳。设计文档 09 节要求合一。
 *
 * ⚠️ 与设计的偏差：文档写「内部三个子页签」，这里只做了两个。
 * 原因：穿透页的「单基金钻取」不是独立页面，它由页面内的搜索框驱动，
 * 而「行业反查」点一下就跳进那个钻取。拆成第三个 tab 要把这份状态提升出去，
 * 还会断掉「反查 → 钻取」这条链路。宁可两个页签也不为了凑数拆坏它。
 * 两个原页面一行未改，随时可以无损拆开。
 */
import { MergedTabs } from "../components/MergedTabs";
import FundPersistencePage from "./FundPersistencePage";
import FundPenetrationPage from "./FundPenetrationPage";

export default function FundResearchPage({ initialTab }: { initialTab?: string }) {
  return (
    <MergedTabs
      defaultKey={initialTab}
      tabs={[
        { key: "persistence", label: "持续性（业绩能不能延续）", node: <FundPersistencePage /> },
        { key: "holding", label: "持仓穿透（超额买了什么）", node: <FundPenetrationPage /> },
      ]}
    />
  );
}
