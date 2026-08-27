import { useState } from "react";
import MarketPage from "./pages/MarketPage";
import BacktestPage from "./pages/BacktestPage";
import PaperPage from "./pages/PaperPage";
import OptimizePage from "./pages/OptimizePage";
import MonitorPage from "./pages/MonitorPage";
import FactorMinePage from "./pages/FactorMinePage";
import { sidebarTheme, useTheme } from "./theme";

type Tab = "market" | "backtest" | "paper" | "optimize" | "monitor" | "factor";

const NAV: { key: Tab; label: string; icon: string }[] = [
  { key: "market", label: "行情看板", icon: "▤" },
  { key: "backtest", label: "策略回测", icon: "≋" },
  { key: "optimize", label: "参数寻优", icon: "◎" },
  { key: "factor", label: "因子挖掘", icon: "∴" },
  { key: "paper", label: "模拟盘", icon: "◷" },
  { key: "monitor", label: "系统监控", icon: "◈" },
];

/** 应用骨架：左侧终端式导航 + 主内容区。 */
export default function App() {
  const [tab, setTab] = useState<Tab>("market");
  const { mode, colors, toggle } = useTheme();
  const sb = sidebarTheme;

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
        {/* 导航 */}
        <nav style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
          {NAV.map((n) => {
            const active = tab === n.key;
            return (
              <button
                key={n.key}
                onClick={() => setTab(n.key)}
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
        {/* 底部：主题切换 + 版本 */}
        <div style={{ borderTop: `1px solid ${sb.divider}`, paddingTop: 12, marginTop: 12 }}>
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
          <div style={{ color: sb.text, fontSize: 10.5, padding: "6px 12px", opacity: 0.6 }}>v0.5 · Docker</div>
        </div>
      </aside>

      {/* —— 主内容区 —— */}
      <main style={{ flex: 1, minWidth: 0, padding: "22px 26px 40px" }}>
        {tab === "market" && <MarketPage />}
        {tab === "backtest" && <BacktestPage />}
        {tab === "optimize" && <OptimizePage />}
        {tab === "factor" && <FactorMinePage />}
        {tab === "paper" && <PaperPage />}
        {tab === "monitor" && <MonitorPage />}
      </main>
    </div>
  );
}
