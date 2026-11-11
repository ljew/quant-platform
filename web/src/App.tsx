import { lazy, Suspense, useEffect, useState } from "react";
import TodayPage from "./pages/TodayPage";
import { Hub } from "./components/Hub";
import { sidebarTheme, useTheme } from "./theme";

// 首页保持直出，其余页面按导航拆分，避免一次性下载全部研究与管理模块。
const BacktestOptimizePage = lazy(() => import("./pages/BacktestOptimizePage"));
const PaperPage = lazy(() => import("./pages/PaperPage"));
const FactorMinePage = lazy(() => import("./pages/FactorMinePage"));
const StrategyOverviewPage = lazy(() => import("./pages/StrategyOverviewPage"));
const ResearchCardsPage = lazy(() => import("./pages/ResearchCardsPage"));
const SignalPage = lazy(() => import("./pages/SignalPage"));
const AttributionPage = lazy(() => import("./pages/AttributionPage"));
const DataPipelinePage = lazy(() => import("./pages/DataPipelinePage"));
const MonitorPage = lazy(() => import("./pages/MonitorPage"));
const AutoResearchHubPage = lazy(() => import("./pages/AutoResearchHubPage"));
const DataHealthPage = lazy(() => import("./pages/DataHealthPage"));
const FundResearchPage = lazy(() => import("./pages/FundResearchPage"));
const NewsHubPage = lazy(() => import("./pages/NewsHubPage"));
const MarketHubPage = lazy(() => import("./pages/MarketHubPage"));
const AgentPortalPage = lazy(() => import("./pages/AgentPortalPage"));
const ApprovalsPage = lazy(() => import("./pages/ApprovalsPage"));

/**
 * 重构 v2.1：13 个二级 Tab → 9 个（2026-10-08 精简）。
 * 回测+寻优、自动研究+驾驶舱、管道+监控 三对重叠页面壳合并（MergedTabs，
 * 原组件未改可无损拆开）；因子检验改名「因子实验室」明确分工（实验室造因子，
 * 研究卡管上岗）。旧 quant-nav tab 名自动映射到新位置。
 */

type HubKey = "today" | "research" | "track" | "data" | "agent";

const HUBS: { key: HubKey; label: string; icon: string; tabs: { key: string; label: string }[] }[] = [
  { key: "today", label: "今日观点", icon: "☀", tabs: [
    { key: "today", label: "今日观点" },
    // 资讯库：全部 / 境外 / 四时段报告 / 公众号 合一（设计文档 08 节：3 个 tab）
    { key: "news", label: "资讯库" },
    // 行情：看板 + 资金流合一
    { key: "market", label: "行情" },
  ] },
  { key: "research", label: "策略研究", icon: "⚗", tabs: [
    // 总览当门面：几条线、各自走到哪、今天该处理什么（设计文档 09 节）
    { key: "strategy-overview", label: "总览" },
    // 「卡」是内部黑话，用户说的是「策略」；因子卡也在这个库里（设计文档 09 节）
    { key: "cards", label: "策略库" },
    // 基金研究：持续性 + 持仓穿透合一（设计文档 09 节）
    { key: "fund-research", label: "基金研究" },
    { key: "btopt", label: "回测寻优" },
    { key: "factorlab", label: "因子实验室" },
    // 信号归因：测的是「信号有没有效」，不涉及资金 —— 属于研究，不属于跟踪（设计文档 10 节）
    { key: "attribution", label: "信号归因" },
  ] },
  { key: "track", label: "组合跟踪", icon: "◷", tabs: [
    // 模拟盘 + 镜像 / 实验合成一张表（设计文档 10 节），tab 名改「组合」
    { key: "paper", label: "组合" },
    { key: "signal", label: "信号组合" },
  ] },
  { key: "data", label: "数据中心", icon: "⇉", tabs: [
    // 健康当门面（设计文档 11 节：第⑦块提到第一位）
    { key: "datahealth", label: "数据健康" },
    // 血缘 / 步骤 / 运行（原「自动研究 → 流水线实况」并进来了）
    { key: "pipeline", label: "数据管道" },
    // 容器 / 调度 / 告警（原来和管道各存一份运行记录，已去重）
    { key: "monitor", label: "系统服务" },
    // ⚠️ 与设计偏差：「自动研究」保留。设计说它的 8 步已搬到策略研究总览，
    // 但那只是**状态横条**；这里还有编排的运行记录、闸门审批、自动化开关、研究纪律 ——
    // 而且它是「8 步 DAG（data→…→publish）」，跟数据管道那条 ETL 是两套东西。
    // 删掉会真丢功能，所以留着，位置降到最后一个。
    { key: "auto", label: "自动研究" },
  ] },
  // 智能体：不是第 5 个工作台，是「谁干活」那一层（设计文档 05 节）。
  // 单 tab，用同域反代把 dsh 嵌进来 —— 补上「两个平台一个入口」的最后一块。
  { key: "agent", label: "智能体", icon: "◎", tabs: [
    { key: "agent", label: "智能体" },
    // 审批中心：状态机早就跑通了，但批准只能走 CLI —— 人不在终端就没法批，
    // 「提议 → 人批 → 带令牌执行」这条链就断在中间。
    { key: "approvals", label: "审批" },
  ] },
];

/** 旧 tab 名 → 新二级 tab（兼容各处 dispatch 的 quant-nav 跳转）。 */
const LEGACY_TAB_MAP: Record<string, string> = {
  backtest: "btopt",      // v2.1 合并
  optimize: "btopt",
  factor: "factorlab",    // 因子检验 → 因子实验室
  cockpit: "auto",        // 研究驾驶舱 → 自动研究 · 流水线实况
  datasys: "pipeline",    // 数据与系统 → 数据管道（拆成三个平级 tab）
  etf_flow: "etfflow",
  etf: "etfflow",
  report: "reports",      // 市场资讯报告
  market_report: "reports",
  foreign: "foreign-news",
  overseas_news: "foreign-news",
  fund: "fund-research",          // 2026-11-10 合并
  "fund-holding": "fund-research",
  "foreign-news": "news",         // 2026-11-10 资讯 4 合 1
  reports: "news",
  wechat: "news",
  etfflow: "market",              // 2026-11-10 行情 2 合 1
};

function findHub(sub: string): { hub: HubKey; sub: string } | null {
  const mapped = LEGACY_TAB_MAP[sub] ?? sub;
  for (const h of HUBS) {
    if (h.tabs.some((t) => t.key === mapped)) return { hub: h.key, sub: mapped };
  }
  return null;
}

/**
 * 读地址栏 hash 作为初始页面：#wechat / #btopt / #cards/384。
 * 第二段是可选的记录 id —— 智能体平台的产物「跳回核对」就靠它：
 * 那边只能拼 URL，派不了 quant-nav 事件（跨进程、跨端口）。
 */
type Nav = { hub: HubKey; sub: string; id?: number; inner?: string };

function parseHash(raw: string): Nav | null {
  const [tab, idRaw, inner] = raw.split("/");
  const hit = tab ? findHub(tab) : null;
  if (!hit) return null;
  const id = Number(idRaw);
  const withId = Number.isFinite(id) && id > 0 ? { ...hit, id } : hit;
  // 第三段是页面内部的子页签（如 #cards/2/backtests）
  return inner ? { ...withId, inner } : withId;
}

function initialNav(): Nav {
  const raw = typeof window !== "undefined" ? window.location.hash.replace(/^#/, "") : "";
  return parseHash(raw) || { hub: "today", sub: "today" };
}

function HubContent({ sub, id, inner }: { sub: string; id?: number; inner?: string }) {
  switch (sub) {
    case "today": return <TodayPage />;
    case "news": return <NewsHubPage />;
    case "market": return <MarketHubPage />;
    case "strategy-overview": return <StrategyOverviewPage />;
    case "cards": return <ResearchCardsPage initialDetailId={id} initialTab={inner} />;
    case "fund-research": return <FundResearchPage initialTab={inner} />;
    case "btopt": return <BacktestOptimizePage />;
    case "factorlab": return <FactorMinePage />;
    case "paper": return <PaperPage />;
    case "signal": return <SignalPage />;
    case "attribution": return <AttributionPage />;
    case "datahealth": return <DataHealthPage />;
    case "pipeline": return <DataPipelinePage />;
    case "monitor": return <MonitorPage />;
    case "auto": return <AutoResearchHubPage />;
    case "agent": return <AgentPortalPage />;
    case "approvals": return <ApprovalsPage />;
    default: return null;
  }
}

/** 应用骨架：左侧 4 入口导航 + 枢纽页（二级 Tab）。 */
export default function App() {
  const [{ hub, sub, id, inner }, setNav] = useState<Nav>(initialNav);
  const setHub = (h: HubKey) => setNav((n) => ({ ...n, hub: h }));
  const setSub = (s: string) => setNav((n) => ({ ...n, sub: s }));
  const { mode, colors, toggle } = useTheme();
  const sb = sidebarTheme;

  // 地址栏 hash 跟随当前页：刷新不回首页、也方便存链接直达（如 /#wechat、/#cards/384）
  useEffect(() => {
    // inner 但无 id 时补一个 0 占位（如 #fund-research/0/holding）：
    // parseHash 里 Number("0") 不 > 0，会被当成「无 id」，但 inner 段能留下，
    // 这样「子页签直达」刷新后不丢。
    const want = id
      ? `${sub}/${id}${inner ? `/${inner}` : ""}`
      : inner ? `${sub}/0/${inner}` : sub;
    if (window.location.hash.replace(/^#/, "") !== want) {
      window.history.replaceState(null, "", `#${want}`);
    }
  }, [sub, id, inner]);

  // 跨页跳转：dispatch(new CustomEvent("quant-nav", { detail: { tab } }))，tab 可为新旧任意 tab 名
  useEffect(() => {
    const onNav = (e: Event) => {
      const detail = (e as CustomEvent<{ tab?: string; id?: number | null; inner?: string }>).detail;
      const t = detail?.tab;
      if (!t) return;
      const hit = findHub(t);
      if (hit) setNav({ ...hit, id: detail?.id ?? undefined, inner: detail?.inner });
    };
    window.addEventListener("quant-nav", onNav as EventListener);
    return () => window.removeEventListener("quant-nav", onNav as EventListener);
  }, []);

  // 地址栏 hash 反向驱动导航。
  // 之前只写了「导航 → hash」单向同步，没有反向监听：
  // 于是 `window.location.hash = "cards/2"` 这类跳转、页面里的 <a href="#...">、
  // 以及浏览器前进/后退，全都不生效 —— 看着有链接，点了没反应。
  // replaceState 不触发 hashchange，所以和我们自己写 hash 的那条不会打架。
  useEffect(() => {
    const onHash = () => {
      const raw = window.location.hash.replace(/^#/, "");
      const hit = parseHash(raw);
      if (!hit) return;
      setNav(hit);
      // 把旧 tab 名就地归一（#foreign-news → #news）。
      // 不能只靠「写 hash」那条副作用：它依赖 [sub,id,inner]，
      // 跳到的目标正好是当前页时状态没变、副作用不跑，地址栏就停在旧名上。
      const canonical = hit.id
        ? `${hit.sub}/${hit.id}${hit.inner ? `/${hit.inner}` : ""}`
        : hit.sub;
      if (raw !== canonical) {
        window.history.replaceState(null, "", `#${canonical}`);
      }
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const activeHub = HUBS.find((h) => h.key === hub)!;

  return (
    <div style={{ display: "flex", minHeight: "100vh", background: colors.bg, color: colors.text }}>
      {/* —— 左侧导航栏 —— */}
      <aside
        style={{
          width: 176,
          flexShrink: 0,
          background: sb.bg,
          display: "flex",
          flexDirection: "column",
          padding: "20px 12px",
          position: "sticky",
          top: 0,
          height: "100vh",
          boxSizing: "border-box",
        }}
      >
        {/* 品牌 */}
        <div style={{ padding: "0 10px 18px", borderBottom: `1px solid ${sb.divider}`, marginBottom: 14 }}>
          <div style={{ color: sb.brand, fontWeight: 700, fontSize: 15.5, letterSpacing: 1 }}>QUANT·DESK</div>
          <div style={{ color: sb.text, fontSize: 11, marginTop: 3, letterSpacing: 2 }}>个人投研平台</div>
        </div>
        {/* 导航：4 个一级入口 */}
        <nav style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
          {HUBS.map((n) => {
            const active = hub === n.key;
            return (
              <button
                key={n.key}
                onClick={() => { setHub(n.key); setSub(n.tabs[0].key); }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  padding: "9px 12px",
                  borderRadius: 8,
                  border: 0,
                  cursor: "pointer",
                  fontSize: 13.5,
                  textAlign: "left",
                  background: active ? sb.activeBg : "transparent",
                  color: active ? sb.activeText : sb.text,
                  fontWeight: active ? 600 : 400,
                  transition: "background .15s,color .15s",
                }}
                onMouseEnter={(e) => { if (!active) e.currentTarget.style.background = sb.hoverBg; }}
                onMouseLeave={(e) => { if (!active) e.currentTarget.style.background = "transparent"; }}
              >
                <span style={{ fontSize: 15, width: 18, textAlign: "center" }}>{n.icon}</span>
                {n.label}
              </button>
            );
          })}
        </nav>
        {/* 底部：文档链接 + 主题切换 + 版本 */}
        <div style={{ borderTop: `1px solid ${sb.divider}`, paddingTop: 12, marginTop: 12 }}>
          <div style={{ padding: "0 12px 8px", display: "flex", flexDirection: "column", gap: 2 }}>
            <a href="/docs/architecture.html" target="_blank" rel="noreferrer"
              style={{ color: sb.text, fontSize: 12.5, textDecoration: "none", padding: "4px 0" }}
              onMouseEnter={(e) => (e.currentTarget.style.color = sb.activeText)}
              onMouseLeave={(e) => (e.currentTarget.style.color = sb.text)}>
              <span style={{ marginRight: 8 }}>📐</span>系统架构设计
            </a>
            <a href="/docs/manual.html" target="_blank" rel="noreferrer"
              style={{ color: sb.text, fontSize: 12.5, textDecoration: "none", padding: "4px 0" }}
              onMouseEnter={(e) => (e.currentTarget.style.color = sb.activeText)}
              onMouseLeave={(e) => (e.currentTarget.style.color = sb.text)}>
              <span style={{ marginRight: 8 }}>📖</span>使用说明书
            </a>
            <a href="/docs/methodology.html" target="_blank" rel="noreferrer"
              style={{ color: sb.text, fontSize: 12.5, textDecoration: "none", padding: "4px 0" }}
              onMouseEnter={(e) => (e.currentTarget.style.color = sb.activeText)}
              onMouseLeave={(e) => (e.currentTarget.style.color = sb.text)}>
              <span style={{ marginRight: 8 }}>📚</span>量化方法论
            </a>
          </div>
          <button
            onClick={toggle}
            style={{
              width: "100%",
              padding: "7px 12px",
              borderRadius: 8,
              border: 0,
              background: "transparent",
              color: sb.text,
              cursor: "pointer",
              fontSize: 12.5,
              textAlign: "left",
            }}
          >
            <span style={{ marginRight: 8 }}>{mode === "dark" ? "☀" : "☾"}</span>
            {mode === "dark" ? "浅色模式" : "暗色模式"}
          </button>
          <div style={{ color: sb.text, fontSize: 10.5, padding: "6px 12px", opacity: 0.6 }}>v2.1 · 四入口九页面 · 本机</div>
        </div>
      </aside>

      {/* —— 主内容区 —— */}
      <main style={{ flex: 1, minWidth: 0, padding: "22px 26px 40px" }}>
        <Hub
          tabs={activeHub.tabs}
          active={sub}
          onChange={setSub}
        >
          <Suspense fallback={<div role="status" style={{ padding: 36, color: colors.muted }}>页面加载中…</div>}>
            <HubContent sub={sub} id={id} inner={inner} />
          </Suspense>
        </Hub>
      </main>
    </div>
  );
}
