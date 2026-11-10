/**
 * 资讯库（今日观点 · Tab）：原先 4 个资讯 tab 合为一个入口、四个子页签。
 *
 * 为什么合并：全部 / 境外 / 报告 / 公众号 的差别只是「来源」和「加工深度」，
 * 不是用法。查一条消息不该先做分类题（设计文档 08 节）。
 *
 * ⚠️ 与设计的偏差：文档要的是「一条时间倒序的流 + 顶部来源筛选器」，
 * 这里先用子页签收口。做真正的合流需要把国内 / 境外 / 报告 / 公众号
 * 四个数据源并成一条流（后端要加统一 feed 接口），是下一步的事。
 * 现在至少入口从 4 个降到 1 个，四个原页面一行未改，可以无损拆开。
 */
import { MergedTabs } from "../components/MergedTabs";
import NewsFeedPage from "./NewsFeedPage";
import ForeignNewsPage from "./ForeignNewsPage";
import ReportsPage from "./ReportsPage";
import WechatPage from "./WechatPage";

export default function NewsHubPage() {
  return (
    <MergedTabs
      tabs={[
        { key: "all", label: "全部（国内源）", node: <NewsFeedPage /> },
        { key: "foreign", label: "境外", node: <ForeignNewsPage /> },
        { key: "reports", label: "四时段报告", node: <ReportsPage /> },
        { key: "wechat", label: "公众号", node: <WechatPage /> },
      ]}
    />
  );
}
