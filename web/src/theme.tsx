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
  bg: "#f4f6fa",
  card: "#ffffff",
  text: "#1f2533",
  muted: "#7a8299",
  border: "#e6eaf2",
  header: "#0e1729",
  headerText: "#f2f5fb",
  up: "#d92c2c",
  down: "#1a8a48",
  accent: "#2456c8",
  tableStripe: "#f2f6fd",
  chartBg: "#ffffff",
  axis: "#667089",
};

export const darkColors: ThemeColors = {
  bg: "#0f131c",
  card: "#171c28",
  text: "#dbe0ea",
  muted: "#8b93a7",
  border: "#262d3d",
  header: "#090d15",
  headerText: "#e8eaf2",
  up: "#ff5257",
  down: "#45b854",
  accent: "#4d82f0",
  tableStripe: "#141b2b",
  chartBg: "#171c28",
  axis: "#8b93a7",
};

/** 侧边栏专属色（明暗主题都用深色，投研终端感） */
export const sidebarTheme = {
  bg: "#0c1220",
  activeBg: "rgba(77,130,240,0.18)",
  activeText: "#6d9dff",
  text: "#96a0b5",
  hoverBg: "rgba(255,255,255,0.05)",
  brand: "#e8ecf5",
  divider: "rgba(255,255,255,0.07)",
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
