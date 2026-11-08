/**
 * W46-1 / M3-7 成本敏感网格面板。
 * 挂载：BacktestPage 净值曲线块下方。选格「应用此成本」→ cost_source=grid_pick 重跑单次回测。
 * 选中预览在应用完成前不覆盖主结果区徽章。
 */
import { useMemo, useState } from "react";
import {
  api,
  CostGridCell,
  CostSensitivityResult,
  CostProfile,
} from "../api/client";
import { ThemeColors } from "../theme";
import { Card, Btn } from "./ui";
import EvalBadges from "./EvalBadges";

export interface CostSensitivityPanelProps {
  symbol: string;
  start: string;
  end: string;
  strategy: string;
  params: Record<string, unknown>;
  colors: ThemeColors;
  /** 应用选格并完成单次回测后回调（主结果区才换徽章） */
  onApplied: (result: import("../api/client").BacktestResult) => void;
  disabled?: boolean;
}

function fmtPct(x: number | undefined | null): string {
  if (x == null || Number.isNaN(x)) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

function fmtWan(x: number): string {
  return `万${(x * 10000).toFixed(1).replace(/\.0$/, "")}`;
}

function metricOf(cell: CostGridCell, metric: string): number {
  if (metric === "sharpe") return cell.sharpe;
  if (metric === "total_return") return cell.total_return;
  return cell.excess_return;
}

function heatColor(v: number, min: number, max: number): string {
  if (max <= min) return "rgba(76,110,245,0.12)";
  const t = Math.max(0, Math.min(1, (v - min) / (max - min)));
  // 红(低) → 黄 → 绿(高)
  const r = Math.round(220 - t * 140);
  const g = Math.round(80 + t * 120);
  const b = Math.round(80 + (1 - Math.abs(t - 0.5) * 2) * 40);
  return `rgba(${r},${g},${b},0.35)`;
}

export default function CostSensitivityPanel({
  symbol,
  start,
  end,
  strategy,
  params,
  colors,
  onApplied,
  disabled,
}: CostSensitivityPanelProps) {
  const [mode, setMode] = useState<"grid" | "multipliers">("grid");
  const [metric, setMetric] = useState<"excess_return" | "sharpe" | "total_return">("excess_return");
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [err, setErr] = useState("");
  const [data, setData] = useState<CostSensitivityResult | null>(null);
  const [picked, setPicked] = useState<CostGridCell | null>(null);

  const run = async () => {
    if (!strategy || disabled) return;
    setLoading(true);
    setErr("");
    setPicked(null);
    try {
      const res = await api.costSensitivity({
        symbol,
        start,
        end,
        strategy,
        params,
        initial_cash: 1_000_000,
        mode,
        metric,
      });
      setData(res);
    } catch (e) {
      setErr((e as Error).message || String(e));
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  const apply = async (cell: CostGridCell) => {
    setApplying(true);
    setErr("");
    try {
      // 同步单次回测写回；cost_source=grid_pick
      const res = await api.backtestSync({
        symbol,
        start,
        end,
        strategy,
        params,
        initial_cash: 1_000_000,
        commission: cell.commission,
        slippage: cell.slippage,
        cost_source: "grid_pick",
      });
      onApplied(res);
      setPicked(cell);
    } catch (e) {
      setErr(`应用失败: ${(e as Error).message || String(e)}`);
    } finally {
      setApplying(false);
    }
  };

  const previewProfile: CostProfile | null = useMemo(() => {
    if (!picked || !data) return null;
    return {
      commission: picked.commission,
      slippage: picked.slippage,
      stamp_tax: data.base_cost_profile?.stamp_tax,
      min_commission: data.base_cost_profile?.min_commission,
      risk_free_rate: data.base_cost_profile?.risk_free_rate,
      source: "grid_pick",
      label: `网格选中·佣${fmtWan(picked.commission)}+滑${fmtWan(picked.slippage)}`,
    };
  }, [picked, data]);

  const gridView = useMemo(() => {
    if (!data || data.mode !== "grid") return null;
    const cs = data.axes.commissions || [];
    const ss = data.axes.slippages || [];
    const map = new Map<string, CostGridCell>();
    for (const cell of data.cells) {
      map.set(`${cell.commission}|${cell.slippage}`, cell);
    }
    const vals = data.cells.map((c) => metricOf(c, data.metric));
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    return { cs, ss, map, min, max };
  }, [data]);

  return (
    <Card
      title="成本敏感（网格）"
      extra={
        <span style={{ fontSize: 12, color: colors.muted }}>
          W46-1 · 默认可扫万2.5+万1 · 长任务未做 async
        </span>
      }
      colors={colors}
    >
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center", marginBottom: 12 }}>
        <label style={{ fontSize: 12.5, color: colors.muted }}>
          模式{" "}
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as "grid" | "multipliers")}
            style={{ marginLeft: 4 }}
          >
            <option value="grid">commission×slippage 网格</option>
            <option value="multipliers">成本×1 / ×2</option>
          </select>
        </label>
        <label style={{ fontSize: 12.5, color: colors.muted }}>
          着色{" "}
          <select
            value={metric}
            onChange={(e) => setMetric(e.target.value as typeof metric)}
            style={{ marginLeft: 4 }}
          >
            <option value="excess_return">超额收益</option>
            <option value="sharpe">夏普</option>
            <option value="total_return">总收益</option>
          </select>
        </label>
        <Btn
          onClick={run}
          disabled={loading || disabled || !strategy}
          small
        >
          {loading ? "扫描中…" : "跑成本网格"}
        </Btn>
        {data?.base_cost_profile && (
          <EvalBadges cost_profile={data.base_cost_profile} colors={colors} />
        )}
      </div>

      {err && (
        <div style={{ color: colors.down, fontSize: 12.5, marginBottom: 10 }}>{err}</div>
      )}

      {data?.summary?.fragile && (
        <div
          style={{
            background: "rgba(200,134,13,0.15)",
            border: "1px solid #c8860d",
            borderRadius: 8,
            padding: "8px 12px",
            fontSize: 12.5,
            color: "#c8860d",
            marginBottom: 10,
          }}
        >
          仅低摩擦成立：默认格超额为正，但成本翻倍后超额≤0。{data.summary.note}
        </div>
      )}

      {data && data.mode === "multipliers" && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ color: colors.muted, textAlign: "left" }}>
                <th style={{ padding: "6px 8px" }}>倍率</th>
                <th>佣金</th><th>滑点</th><th>超额</th><th>夏普</th><th>总收益</th><th></th>
              </tr>
            </thead>
            <tbody>
              {data.cells.map((cell) => (
                <tr
                  key={`${cell.commission}-${cell.slippage}-${cell.multiplier}`}
                  style={{
                    borderTop: `1px solid ${colors.border}`,
                    outline: cell.is_default ? `2px solid ${colors.accent}` : undefined,
                  }}
                >
                  <td style={{ padding: "6px 8px" }}>
                    ×{cell.multiplier ?? "—"}
                    {cell.is_default ? " · 默认" : ""}
                  </td>
                  <td className="num">{fmtWan(cell.commission)}</td>
                  <td className="num">{fmtWan(cell.slippage)}</td>
                  <td className="num">{fmtPct(cell.excess_return)}</td>
                  <td className="num">{cell.sharpe.toFixed(2)}</td>
                  <td className="num">{fmtPct(cell.total_return)}</td>
                  <td>
                    <Btn
                      kind="ghost"
                      small
                      disabled={applying}
                      onClick={() => apply(cell)}
                    >
                      {applying ? "…" : "应用此成本"}
                    </Btn>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {gridView && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", fontSize: 12.5, minWidth: 420 }}>
            <thead>
              <tr>
                <th style={{ padding: 6, color: colors.muted, textAlign: "left" }}>佣 \\ 滑</th>
                {gridView.ss.map((s) => (
                  <th key={s} style={{ padding: 6, color: colors.muted, fontWeight: 500 }}>
                    {fmtWan(s)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {gridView.cs.map((c) => (
                <tr key={c}>
                  <td style={{ padding: 6, color: colors.muted }}>{fmtWan(c)}</td>
                  {gridView.ss.map((s) => {
                    const cell = gridView.map.get(`${c}|${s}`);
                    if (!cell) {
                      // 默认格可能在轴外追加
                      const extra = data!.cells.find(
                        (x) => x.commission === c && x.slippage === s,
                      );
                      if (!extra) {
                        return (
                          <td key={s} style={{ padding: 4, background: colors.card }}>
                            —
                          </td>
                        );
                      }
                    }
                    const use = cell || data!.cells.find((x) => x.commission === c && x.slippage === s)!;
                    const v = metricOf(use, data!.metric);
                    const isDef = !!use.is_default;
                    const isPick =
                      picked &&
                      picked.commission === use.commission &&
                      picked.slippage === use.slippage;
                    return (
                      <td
                        key={s}
                        title={`超额 ${fmtPct(use.excess_return)} · 夏普 ${use.sharpe.toFixed(2)}${isDef ? " · 默认" : ""}`}
                        style={{
                          padding: 6,
                          textAlign: "center",
                          background: heatColor(v, gridView.min, gridView.max),
                          outline: isDef
                            ? `2px solid ${colors.accent}`
                            : isPick
                              ? `2px dashed ${colors.up}`
                              : undefined,
                          cursor: "pointer",
                          borderRadius: 4,
                        }}
                        onClick={() => setPicked(use)}
                      >
                        <div style={{ fontWeight: 600 }}>
                          {data!.metric === "sharpe" ? v.toFixed(2) : fmtPct(v)}
                        </div>
                        {isDef && (
                          <div style={{ fontSize: 10, color: colors.accent }}>默认</div>
                        )}
                        <button
                          type="button"
                          disabled={applying}
                          onClick={(e) => {
                            e.stopPropagation();
                            apply(use);
                          }}
                          style={{
                            marginTop: 4,
                            fontSize: 11,
                            border: `1px solid ${colors.border}`,
                            background: colors.card,
                            color: colors.text,
                            borderRadius: 4,
                            padding: "2px 6px",
                            cursor: applying ? "not-allowed" : "pointer",
                          }}
                        >
                          应用
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          {/* 轴外强制默认格提示 */}
          {data!.cells.some(
            (c) =>
              c.is_default &&
              !(data!.axes.commissions || []).includes(c.commission),
          ) && (
            <div style={{ marginTop: 8, fontSize: 12, color: colors.muted }}>
              已追加默认格（万2.5+万1），虽不在自定义轴上。
            </div>
          )}
        </div>
      )}

      {previewProfile && (
        <div style={{ marginTop: 12, fontSize: 12.5, color: colors.muted }}>
          已选预览（未覆盖主结果徽章，直至应用完成）：{" "}
          <EvalBadges cost_profile={previewProfile} colors={colors} />
        </div>
      )}

      {data?.summary && (
        <div style={{ marginTop: 10, fontSize: 12, color: colors.muted }}>
          默认格超额{data.summary.default_cell_ok ? ">" : "≤"}0 · 加倍超额
          {data.summary.double_cost_excess_positive ? ">" : "≤"}0
          {data.summary.note ? ` · ${data.summary.note}` : ""}
        </div>
      )}
    </Card>
  );
}
