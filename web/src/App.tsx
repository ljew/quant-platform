import { useState } from "react";
import MarketPage from "./pages/MarketPage";
import BacktestPage from "./pages/BacktestPage";
import PaperPage from "./pages/PaperPage";
import OptimizePage from "./pages/OptimizePage";
import MonitorPage from "./pages/MonitorPage";
import FactorMinePage from "./pages/FactorMinePage";
import { useTheme } from "./theme";

type Tab = "market" | "backtest" | "paper" | "optimize" | "monitor" | "factor";

/** 应用骨架：顶部导航（主题切换）+ 页面切换。 */
export default function App() {
  const [tab, setTab] = useState<Tab>("market");
  const { mode, colors, toggle } = useTheme();

  return (
    <div style={{ minHeight: "100vh", background: colors.bg, color: colors.text }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "0 20px",
          height: 52,
          background: colors.header,
          color: colors.headerText,
        }}
      >
        <span style={{ fontSize: 16, fontWeight: 700, marginRight: 24 }}>量化投研平台</span>
        <NavBtn active={tab === "market"} onClick={() => setTab("market")} colors={colors}>
          行情看板
        </NavBtn>
        <NavBtn active={tab === "backtest"} onClick={() => setTab("backtest")} colors={colors}>
          策略回测
        </NavBtn>
        <NavBtn active={tab === "optimize"} onClick={() => setTab("optimize")} colors={colors}>
          参数寻优
        </NavBtn>
        <NavBtn active={tab === "paper"} onClick={() => setTab("paper")} colors={colors}>
          模拟盘
        </NavBtn>
        <NavBtn active={tab === "monitor"} onClick={() => setTab("monitor")} colors={colors}>
          监控
        </NavBtn>
        <NavBtn active={tab === "factor"} onClick={() => setTab("factor")} colors={colors}>
          因子挖掘
        </NavBtn>
        <span style={{ flex: 1 }} />
        <button
          onClick={toggle}
          title="切换明暗主题"
          style={{
            padding: "4px 12px",
            borderRadius: 6,
            border: "1px solid rgba(255,255,255,.35)",
            background: "transparent",
            color: colors.headerText,
            cursor: "pointer",
            fontSize: 13,
          }}
        >
          {mode === "dark" ? "☀ 浅色" : "🌙 暗色"}
        </button>
        <span style={{ fontSize: 12, opacity: 0.7, marginLeft: 8 }}>v0.4</span>
      </header>
      <main>
        {tab === "market" && <MarketPage />}
        {tab === "backtest" && <BacktestPage />}
        {tab === "optimize" && <OptimizePage />}
        {tab === "paper" && <PaperPage />}
        {tab === "monitor" && <MonitorPage />}
        {tab === "factor" && <FactorMinePage />}
      </main>
    </div>
  );
}

function NavBtn({
  active,
  onClick,
  children,
  colors,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  colors: { accent: string; header: string };
}) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: "6px 16px",
        borderRadius: 6,
        border: 0,
        background: active ? colors.accent : "transparent",
        color: "#fff",
        cursor: "pointer",
        fontSize: 14,
      }}
    >
      {children}
    </button>
  );
}
