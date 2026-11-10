/**
 * 行情（今日观点 · Tab）：行情看板 + 资金流合为一个入口、两个子页签。
 *
 * 为什么合并：两个页面看的是同一件事的两面 —— 价格走到哪、钱往哪走。
 * 原来平级两个 tab，看指数和看 ETF 资金要来回切（设计文档 08 节）。
 *
 * 子页签标签直接写出各自回答的问题，省得靠 tab 名猜。
 * 原页面一行未改，可无损拆开。
 */
import { MergedTabs } from "../components/MergedTabs";
import MarketPage from "./MarketPage";
import EtfFlowPage from "./EtfFlowPage";

export default function MarketHubPage() {
  return (
    <MergedTabs
      tabs={[
        { key: "kline", label: "指数 / 个股 K 线", node: <MarketPage /> },
        { key: "etf", label: "ETF 资金流（宽基 20 只）", node: <EtfFlowPage /> },
      ]}
    />
  );
}
