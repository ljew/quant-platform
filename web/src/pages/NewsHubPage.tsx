/**
 * 资讯库（今日观点 · Tab）：从「4 个子页签收口」升级成「一条流 + 来源筛选」。
 *
 * 2026-11-11 补：设计文档 08 节要的是「一条时间倒序的流 + 顶部来源筛选器」，
 * 之前先用子页签收口是权宜。现在后端有了 /news/stream（news_feed 的国内+境外、
 * market_reports 的时段报告、knowledge.db 的公众号全文，三路合流），
 * 默认落点就是那条流。
 *
 * 只保留两个子页：它们不只是「另一个来源」，还带各自的操作面 ——
 * 公众号那页有采集链路和凭证管理（Cookie 过期要在这里换），
 * 时段报告那页能看单份报告的完整正文。
 */
import { MergedTabs } from "../components/MergedTabs";
import NewsStreamPage from "./NewsStreamPage";
import ReportsPage from "./ReportsPage";
import WechatPage from "./WechatPage";

export default function NewsHubPage() {
  return (
    <MergedTabs
      tabs={[
        { key: "stream", label: "资讯流", node: <NewsStreamPage /> },
        { key: "wechat", label: "公众号", node: <WechatPage /> },
        { key: "reports", label: "时段报告", node: <ReportsPage /> },
      ]}
    />
  );
}
