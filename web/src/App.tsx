import { lazy, Suspense, useEffect, useState } from "react";
import TodayPage from "./pages/TodayPage";
import { Hub } from "./components/Hub";
import { sidebarTheme, useTheme } from "./theme";

// 首页保持直出，其余页面按导航拆分，避免一次性下载全部研究与管理模块。
const NewsFeedPage = lazy(() => import("./pages/NewsFeedPage"));
const ReportsPage = lazy(() => import("./pages/ReportsPage"));
const MarketPage = lazy(() => import("./pages/MarketPage"));
const BacktestOptimizePage = lazy(() => import("./pages/BacktestOptimizePage"));
const PaperPage = lazy(() => import("./pages/PaperPage"));
const FactorMinePage = lazy(() => import("./pages/FactorMinePage"));
const ResearchCardsPage = lazy(() => import("./pages/ResearchCardsPage"));
const SignalPage = lazy(() => import("./pages/SignalPage"));
const EtfFlowPage = lazy(() => import("./pages/EtfFlowPage"));
const AttributionPage = lazy(() => import("./pages/AttributionPage"));
const AutoResearchHubPage = lazy(() => import("./pages/AutoResearchHubPage"));
const DataSystemPage = lazy(() => import("./pages/DataSystemPage"));
const WechatPage = lazy(() => import("./pages/WechatPage"));

/**
 * 重构 v2.1：13 个二级 Tab → 9 个（2026-10-08 精简）。
 * 回测+寻优、自动研究+驾驶舱、管道+监控 三对重叠页面壳合并（MergedTabs，
 * 原组件未改可无损拆开）；因子检验改名「因子实验室」明确分工（实验室造因子，
 * 研究卡管上岗）。旧 quant-nav tab 名自动映射到新位置。
 */

type HubKey = "today" | "research" | "track" | "data";

const HUBS: { key: HubKey; label: string; icon: string; tabs: { key: string; label: string }[] }[] = [
  { key: "today", label: "今日观点", icon: "☀", tabs: [
    { key: "today", label: "今日观点" },
    // 全部资讯：各来源原文归一（原始信息）。首页只放简报（判断），原文来这看。
    { key: "news", label: "全部资讯" },
    // 报告：四时段跨市场资讯简报，生成即入库（market_reports），可检索可读全文
    { key: "reports", label: "报告" },
    { key: "market", label: "行情看板" },
    // 资金流：20 只宽基 ETF 的日度/分钟资金观察（观察口径，非信号；信号见 NSI）
    { key: "etfflow", label: "资金流" },
    { key: "wechat", label: "公众号" },
  ] },
  { key: "research", label: "策略研究", icon: "⚗", tabs: [
    { key: "cards", label: "研究卡" },
    { key: "btopt", label: "回测寻优" },
    { key: "factorlab", label: "因子实验室" },
  ] },
  { key: "track", label: "组合跟踪", icon: "◷", tabs: [
    { key: "paper", label: "模拟盘" },
    { key: "signal", label: "信号组合" },
    { key: "attribution", label: "归因分析" },
  ] },
  { key: "data", label: "数据中心", icon: "⇉", tabs: [
    { key: "auto", label: "自动研究" },
    { key: "datasys", label: "数据与系统" },
  ] },
];

/** 旧 tab 名 → 新二级 tab（兼容各处 dispatch 的 quant-nav 跳转）。 */
const LEGACY_TAB_MAP: Record<string, string> = {
  backtest: "btopt",      // v2.1 合并
  optimize: "btopt",
  factor: "factorlab",    // 因子检验 → 因子实验室
  cockpit: "auto",        // 研究驾驶舱 → 自动研究 · 流水线实况
  pipeline: "datasys",
  monitor: "datasys",
  etf_flow: "etfflow",
  etf: "etfflow",
  report: "reports",      // 市场资讯报告
  market_report: "reports",
};

function findHub(sub: string): { hub: HubKey; sub: string } | null {
  const mapped = LEGACY_TAB_MAP[sub] ?? sub;
  for (const h of HUBS) {
    if (h.tabs.some((t) => t.key === mapped)) return { hub: h.key, sub: mapped };
  }
  return null;
}

/** 读地址栏 hash 作为初始页面：#wechat / #btopt 等，非法值回落到今日观点。 */
function initialNav(): { hub: HubKey; sub: string } {
  const raw = typeof window !== "undefined" ? window.location.hash.replace(/^#/, "") : "";
  return (raw && findHub(raw)) || { hub: "today", sub: "today" };
}

function HubContent({ sub }: { sub: string }) {
  switch (sub) {
    case "today": return <TodayPage />;
    case "news": return <NewsFeedPage />;
    case "reports": return <ReportsPage />;
    case "market": return <MarketPage />;
    case "etfflow": return <EtfFlowPage />;
    case "wechat": return <WechatPage />;
    case "cards": return <ResearchCardsPage />;
    case "btopt": return <BacktestOptimizePage />;
    case "factorlab": return <FactorMinePage />;
    case "paper": return <PaperPage />;
    case "signal": return <SignalPage />;
    case "attribution": return <AttributionPage />;
    case "auto": return <AutoResearchHubPage />;
    case "datasys": return <DataSystemPage />;
    default: return null;
  }
}

/** 应用骨架：左侧 4 入口导航 + 枢纽页（二级 Tab）。 */
export default function App() {
  const [{ hub, sub }, setNav] = useState<{ hub: HubKey; sub: string }>(initialNav);
  const setHub = (h: HubKey) => setNav((n) => ({ ...n, hub: h }));
  const setSub = (s: string) => setNav((n) => ({ ...n, sub: s }));
  const { mode, colors, toggle } = useTheme();
  const sb = sidebarTheme;

  // 地址栏 hash 跟随当前页：刷新不回首页、也方便存链接直达（如 /#wechat）
  useEffect(() => {
    if (window.location.hash.replace(/^#/, "") !== sub) {
      window.history.replaceState(null, "", `#${sub}`);
    }
  }, [sub]);

  // 跨页跳转：dispatch(new CustomEvent("quant-nav", { detail: { tab } }))，tab 可为新旧任意 tab 名
  useEffect(() => {
    const onNav = (e: Event) => {
      const t = (e as CustomEvent<{ tab?: string }>).detail?.tab;
      if (!t) return;
      const hit = findHub(t);
      if (hit) { setHub(hit.hub); setSub(hit.sub); }
    };
    window.addEventListener("quant-nav", onNav as EventListener);
    return () => window.removeEventListener("quant-nav", onNav as EventListener);
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
            <HubContent sub={sub} />
          </Suspense>
        </Hub>
      </main>
    </div>
  );
}
