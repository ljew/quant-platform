/**
 * W46-2 / M3-3 滚动 OOS（walk-forward）面板。
 * 挂载：BacktestPage 净值 + 成本网格之后（三块之③）。
 * 投影 oos(final) 到主区 EvalBadges；不覆盖净值曲线；不改 W46-1 成本块。
 */
import { useState } from "react";
import {
  api,
  WalkForwardResult,
  WalkForwardWindow,
  EvalOos,
} from "../api/client";
import { ThemeColors } from "../theme";
import { Card, Btn } from "./ui";
import EvalBadges from "./EvalBadges";

export interface WalkForwardPanelProps {
  symbol: string;
  start: string;
  end: string;
  strategy: string;
  params: Record<string, unknown>;
  colors: ThemeColors;
  disabled?: boolean;
  /** 将 WF final oos 投影到主结果区徽章（不改净值） */
  onOosProjected?: (oos: EvalOos) => void;
}

function fmtPct(x: number | undefined | null): string {
  if (x == null || Number.isNaN(x)) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

function verdictColor(v: string, colors: ThemeColors): string {
  if (v === "通过") return "#2f9e44";
  if (v === "过拟合" || v === "未通过" || v === "样本不足") return "#c8860d";
  return colors.muted;
}

function metricOf(w: WalkForwardWindow, side: "is" | "oos", metric: string): number {
  const seg = side === "is" ? w.is : w.oos;
  return metric === "excess_return" ? seg.excess_return : seg.sharpe;
}

export default function WalkForwardPanel({
  symbol,
  start,
  end,
  strategy,
  params,
  colors,
  disabled,
  onOosProjected,
}: WalkForwardPanelProps) {
  const [mode, setMode] = useState<"walk_forward" | "single">("walk_forward");
  const [metric, setMetric] = useState<"sharpe" | "excess_return">("sharpe");
  const [nWindows, setNWindows] = useState(3);
  const [oosRatio, setOosRatio] = useState(0.3);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [data, setData] = useState<WalkForwardResult | null>(null);

  const run = async () => {
    if (!strategy || disabled) return;
    setLoading(true);
    setErr("");
    try {
      const res = await api.walkForward({
        symbol,
        start,
        end,
        strategy,
        params,
        initial_cash: 1_000_000,
        mode,
        metric,
        n_windows: nWindows,
        oos_ratio: oosRatio,
        scheme: "expanding",
      });
      setData(res);
      if (res.oos && onOosProjected) {
        onOosProjected(res.oos);
      }
    } catch (e) {
      setErr((e as Error).message || String(e));
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card
      title="滚动验证（walk-forward OOS）"
      extra={
        <span style={{ fontSize: 12, color: colors.muted }}>
          不改上方净值 · 总门禁 ≥ 单次 oos_ratio
        </span>
      }
      colors={colors}
    >
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center", marginBottom: 10 }}>
        <label style={{ fontSize: 12, color: colors.muted }}>
          模式{" "}
          <select
            value={mode}
            disabled={loading || disabled}
            onChange={(e) => setMode(e.target.value as "walk_forward" | "single")}
            style={{ marginLeft: 4 }}
          >
            <option value="walk_forward">walk_forward</option>
            <option value="single">single</option>
          </select>
        </label>
        <label style={{ fontSize: 12, color: colors.muted }}>
          窗数{" "}
          <select
            value={nWindows}
            disabled={loading || disabled || mode === "single"}
            onChange={(e) => setNWindows(Number(e.target.value))}
            style={{ marginLeft: 4 }}
          >
            {[1, 2, 3, 4, 5, 6].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </label>
        <label style={{ fontSize: 12, color: colors.muted }}>
          oos_ratio{" "}
          <select
            value={oosRatio}
            disabled={loading || disabled}
            onChange={(e) => setOosRatio(Number(e.target.value))}
            style={{ marginLeft: 4 }}
          >
            {[0, 0.2, 0.3, 0.4, 0.5, 0.6].map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </label>
        <label style={{ fontSize: 12, color: colors.muted }}>
          主度量{" "}
          <select
            value={metric}
            disabled={loading || disabled}
            onChange={(e) => setMetric(e.target.value as "sharpe" | "excess_return")}
            style={{ marginLeft: 4 }}
          >
            <option value="sharpe">sharpe</option>
            <option value="excess_return">excess_return</option>
          </select>
        </label>
        <Btn onClick={run} disabled={loading || disabled || !strategy}>
          {loading ? "计算中…" : "跑滚动 OOS"}
        </Btn>
      </div>

      {err && (
        <div style={{ color: "#c92a2a", fontSize: 13, marginBottom: 8 }}>{err}</div>
      )}

      {data && (
        <>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", marginBottom: 10 }}>
            <EvalBadges oos={data.oos} cost_profile={data.cost_profile} colors={colors} />
            <span style={{ fontSize: 12.5, color: colors.muted }}>
              final=<b style={{ color: verdictColor(data.summary.final_verdict, colors) }}>{data.summary.final_verdict}</b>
              {" · "}agg={data.summary.agg_verdict}
              {" · "}legacy={data.summary.legacy_verdict}
              {data.summary.fragile_regime ? " · fragile" : ""}
              {data.summary.gate_ok ? " · gate✓" : " · gate✗"}
            </span>
          </div>

          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ color: colors.muted, textAlign: "left" }}>
                  <th style={{ padding: "6px 8px" }}>窗</th>
                  <th>IS 区间</th>
                  <th>OOS 区间</th>
                  <th>IS {data.metric}</th>
                  <th>OOS {data.metric}</th>
                  <th>OOS 收益</th>
                  <th>判定</th>
                </tr>
              </thead>
              <tbody>
                {data.windows.map((w) => (
                  <tr key={w.index} style={{ borderTop: `1px solid ${colors.border}` }}>
                    <td style={{ padding: "6px 8px" }}>#{w.index}</td>
                    <td style={{ color: colors.muted }}>{w.is_start} ~ {w.is_end}</td>
                    <td style={{ color: colors.muted }}>{w.oos_start} ~ {w.oos_end}</td>
                    <td className="num">{metricOf(w, "is", data.metric).toFixed(2)}</td>
                    <td className="num">{metricOf(w, "oos", data.metric).toFixed(2)}</td>
                    <td className="num">{fmtPct(w.oos.total_return)}</td>
                    <td style={{ color: verdictColor(w.verdict, colors), fontWeight: 600 }}
                      title={w.note || ""}>
                      {w.verdict}
                    </td>
                  </tr>
                ))}
                {data.legacy_single && (
                  <tr style={{ borderTop: `2px solid ${colors.border}`, background: "rgba(76,110,245,0.06)" }}>
                    <td style={{ padding: "6px 8px" }}>legacy</td>
                    <td style={{ color: colors.muted }} colSpan={1}>
                      ~ {data.legacy_single.split_date}
                    </td>
                    <td style={{ color: colors.muted }}>
                      {data.legacy_single.oos_start} ~
                    </td>
                    <td className="num">
                      {(data.metric === "excess_return"
                        ? data.legacy_single.is.excess_return
                        : data.legacy_single.is.sharpe
                      ).toFixed(2)}
                    </td>
                    <td className="num">
                      {(data.metric === "excess_return"
                        ? data.legacy_single.oos.excess_return
                        : data.legacy_single.oos.sharpe
                      ).toFixed(2)}
                    </td>
                    <td className="num">{fmtPct(data.legacy_single.oos.total_return)}</td>
                    <td style={{ color: verdictColor(data.legacy_single.verdict, colors), fontWeight: 600 }}
                      title={data.legacy_single.note || ""}>
                      {data.legacy_single.verdict}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {data.oos?.windows_summary && (
            <div style={{ marginTop: 8, fontSize: 12, color: colors.muted }}>
              {String(data.oos.windows_summary)}
              {data.oos.note ? ` · ${String(data.oos.note)}` : ""}
            </div>
          )}
        </>
      )}
    </Card>
  );
}
