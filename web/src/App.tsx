import { useEffect, useState } from "react";
import TodayPage from "./pages/TodayPage";
import MarketPage from "./pages/MarketPage";
import BacktestPage from "./pages/BacktestPage";
import PaperPage from "./pages/PaperPage";
import OptimizePage from "./pages/OptimizePage";
import MonitorPage from "./pages/MonitorPage";
import DataPipelinePage from "./pages/DataPipelinePage";
import FactorMinePage from "./pages/FactorMinePage";
import ResearchPage from "./pages/ResearchPage";
import SignalPage from "./pages/SignalPage";
import AttributionPage from "./pages/AttributionPage";
import AutoResearchPage from "./pages/AutoResearchPage";
import { Hub } from "./components/Hub";
import { sidebarTheme, useTheme } from "./theme";

/**
 * 重构 v2.0：11 个平级页面 → 4 个一级入口，按「每日使用路径」组织。
 * 今日观点（默认页，打开即有观点）/ 策略研究（回测·寻优·因子一个工作台）/
 * 组合跟踪（模拟盘·信号组合·归因）/ 数据中心（管道·监控·驾驶舱，退居后台）。
 * 旧页面组件全部保留为二级 Tab；旧 quant-nav tab 名自动映射到新位置。
 */

type HubKey = "today" | "research" | "track" | "data";

const HUBS: { key: HubKey; label: string; icon: string; tabs: { key: string; label: string }[] }[] = [
  { key: "today", label: "今日观点", icon: "☀", tabs: [
    { key: "today", label: "今日观点" },
    { key: "market", label: "行情看板" },
  ] },
  { key: "research", label: "策略研究", icon: "⚗", tabs: [
    { key: "backtest", label: "回测" },
    { key: "optimize", label: "参数寻优" },
    { key: "factor", label: "因子检验" },
  ] },
  { key: "track", label: "组合跟踪", icon: "◷", tabs: [
    { key: "paper", label: "模拟盘" },
    { key: "signal", label: "信号组合" },
    { key: "attribution", label: "归因分析" },
  ] },
  { key: "data", label: "数据中心", icon: "⇉", tabs: [
    { key: "auto", label: "自动研究" },
    { key: "pipeline", label: "数据管道" },
    { key: "monitor", label: "系统监控" },
    { key: "cockpit", label: "研究驾驶舱" },
  ] },
];

/** 旧 tab 名 → 新二级 tab（兼容各处 dispatch 的 quant-nav 跳转）。 */
const LEGACY_TAB_MAP: Record<string, string> = {
  news: "today",          // 资讯中心 → 今日观点（已收编为快讯栏）
  cockpit: "cockpit",
};

function findHub(sub: string): { hub: HubKey; sub: string } | null {
  const mapped = LEGACY_TAB_MAP[sub] ?? sub;
  for (const h of HUBS) {
    if (h.tabs.some((t) => t.key === mapped)) return { hub: h.key, sub: mapped };
  }
  return null;
}

function HubContent({ sub }: { sub: string }) {
  switch (sub) {
    case "today": return <TodayPage />;
    case "market": return <MarketPage />;
    case "backtest": return <BacktestPage />;
    case "optimize": return <OptimizePage />;
    case "factor": return <FactorMinePage />;
    case "paper": return <PaperPage />;
    case "signal": return <SignalPage />;
    case "attribution": return <AttributionPage />;
    case "auto": return <AutoResearchPage />;
    case "pipeline": return <DataPipelinePage />;
    case "monitor": return <MonitorPage />;
    case "cockpit": return <ResearchPage />;
    default: return null;
  }
}

/** 应用骨架：左侧 4 入口导航 + 枢纽页（二级 Tab）。 */
export default function App() {
  const [hub, setHub] = useState<HubKey>("today");
  const [sub, setSub] = useState<string>("today");
  const { mode, colors, toggle } = useTheme();
  const sb = sidebarTheme;

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
          <div style={{ color: sb.text, fontSize: 10.5, padding: "6px 12px", opacity: 0.6 }}>v2.0 · 四入口 · 本机</div>
        </div>
      </aside>

      {/* —— 主内容区 —— */}
      <main style={{ flex: 1, minWidth: 0, padding: "22px 26px 40px" }}>
        <Hub
          tabs={activeHub.tabs}
          active={sub}
          onChange={setSub}
        >
          <HubContent sub={sub} />
        </Hub>
      </main>
    </div>
  );
}
