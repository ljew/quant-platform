import { useState } from "react";
import MarketPage from "./pages/MarketPage";
import BacktestPage from "./pages/BacktestPage";
import PaperPage from "./pages/PaperPage";

type Tab = "market" | "backtest" | "paper";

/** 应用骨架：顶部导航 + 页面切换。 */
export default function App() {
  const [tab, setTab] = useState<Tab>("market");

  return (
    <div style={{ minHeight: "100vh", background: "#f5f7fa" }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "0 20px",
          height: 52,
          background: "#001529",
          color: "#fff",
        }}
      >
        <span style={{ fontSize: 16, fontWeight: 700, marginRight: 24 }}>量化投研平台</span>
        <NavBtn active={tab === "market"} onClick={() => setTab("market")}>
          行情看板
        </NavBtn>
        <NavBtn active={tab === "backtest"} onClick={() => setTab("backtest")}>
          策略回测
        </NavBtn>
        <NavBtn active={tab === "paper"} onClick={() => setTab("paper")}>
          模拟盘
        </NavBtn>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 12, opacity: 0.7 }}>React 完整版前端（v0.2）</span>
      </header>
      <main>
        {tab === "market" && <MarketPage />}
        {tab === "backtest" && <BacktestPage />}
        {tab === "paper" && <PaperPage />}
      </main>
    </div>
  );
}

function NavBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: "6px 16px",
        borderRadius: 6,
        border: 0,
        background: active ? "#1668dc" : "transparent",
        color: "#fff",
        cursor: "pointer",
        fontSize: 14,
      }}
    >
      {children}
    </button>
  );
}
