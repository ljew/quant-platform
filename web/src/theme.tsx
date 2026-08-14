import { createContext, useContext, useEffect, useState, ReactNode } from "react";

/** 主题（浅色/暗色）。通过 Context 提供颜色 token，组件 useTheme() 取色。 */

export interface ThemeColors {
  bg: string;        // 页面背景
  card: string;      // 卡片/面板背景
  text: string;      // 主文字
  muted: string;     // 次级文字
  border: string;    // 边框
  header: string;    // 顶栏背景
  headerText: string;
  up: string;        // 涨（A股红）
  down: string;      // 跌（A股绿）
  accent: string;    // 主色（蓝）
  tableStripe: string;
  chartBg: string;   // 图表背景
  axis: string;      // 图表坐标轴文字
}

export const lightColors: ThemeColors = {
  bg: "#f5f7fa",
  card: "#ffffff",
  text: "#333333",
  muted: "#888888",
  border: "#e8e8e8",
  header: "#001529",
  headerText: "#ffffff",
  up: "#cf1322",
  down: "#237804",
  accent: "#1668dc",
  tableStripe: "#f0f7ff",
  chartBg: "#ffffff",
  axis: "#666666",
};

export const darkColors: ThemeColors = {
  bg: "#12151c",
  card: "#1d2130",
  text: "#d8dbe4",
  muted: "#8b90a0",
  border: "#2c3040",
  header: "#0c0f17",
  headerText: "#e8eaf2",
  up: "#ff4d4f",
  down: "#49aa19",
  accent: "#3b82f6",
  tableStripe: "#1a2336",
  chartBg: "#1d2130",
  axis: "#8b90a0",
};

type ThemeMode = "light" | "dark";

const ThemeCtx = createContext<{ mode: ThemeMode; colors: ThemeColors; toggle: () => void }>({
  mode: "light",
  colors: lightColors,
  toggle: () => {},
});

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(() => {
    const saved = localStorage.getItem("quant-theme");
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });

  useEffect(() => {
    localStorage.setItem("quant-theme", mode);
    document.documentElement.setAttribute("data-theme", mode);
  }, [mode]);

  return (
    <ThemeCtx.Provider
      value={{ mode, colors: mode === "dark" ? darkColors : lightColors, toggle: () => setMode((m) => (m === "dark" ? "light" : "dark")) }}
    >
      {children}
    </ThemeCtx.Provider>
  );
}

export function useTheme() {
  return useContext(ThemeCtx);
}
