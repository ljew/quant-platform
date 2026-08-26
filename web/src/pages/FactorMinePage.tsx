import { useCallback, useEffect, useState } from "react";
import EChart from "../components/EChart";
import { api, FactorMineReport, FactorMineSummary } from "../api/client";
import { useTheme } from "../theme";

/** 因子挖掘页：自定义表达式 → IC/ICIR/分组单调/多空/相关性检验。 */
export default function FactorMinePage() {
  const { colors } = useTheme();
  const [expr, setExpr] = useState("safe_inv(pe_ttm, 0, 1000) - roc(c_m, 60)");
  const [name, setName] = useState("自定义因子");
  const [start, setStart] = useState("2024-01-01");
  const [end, setEnd] = useState("2026-08-24");
  const [groups, setGroups] = useState(5);
  const [forward, setForward] = useState(20);
  const [fns, setFns] = useState<Record<string, Record<string, string>>>({});
  const [showFns, setShowFns] = useState(false);
  const [valid, setValid] = useState<{ ok: boolean; error?: string; sample_value?: number | null } | null>(null);
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState<FactorMineReport | null>(null);
  const [history, setHistory] = useState<FactorMineSummary[]>([]);
  const [error, setError] = useState("");

  const loadHistory = useCallback(() => {
    api.factorMineResults(15).then(setHistory).catch(() => {});
  }, []);

  useEffect(() => {
    api.factorFunctions().then(setFns).catch(() => {});
    loadHistory();
  }, [loadHistory]);

  const doValidate = async () => {
    setValid(null);
    try {
      setValid(await api.factorValidate(expr));
    } catch (e) {
      setValid({ ok: false, error: (e as Error).message });
    }
  };

  const runMine = async () => {
    setRunning(true);
    setError("");
    setReport(null);
    try {
      const r = await api.factorMine({ expr, name, start, end, groups, forward });
      setReport(r);
      loadHistory();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  };

  const loadDetail = async (id: number) => {
    try {
      setReport(await api.factorMineDetail(id));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const del = async (id: number) => {
    if (!window.confirm(`删除挖掘结果 #${id}？`)) return;
    try {
      await api.factorMineDelete(id);
      loadHistory();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div style={{ padding: 16 }}>
      {/* 表达式输入 */}
      <div style={{ background: colors.card, borderRadius: 10, padding: 14, border: `1px solid ${colors.border}`, marginBottom: 12 }}>
        <div style={{ fontWeight: 700, marginBottom: 8 }}>因子表达式（自定义挖掘）</div>
        <textarea
          value={expr}
          onChange={(e) => { setExpr(e.target.value); setValid(null); }}
          rows={2}
          style={{ width: "100%", padding: "8px 10px", borderRadius: 6, border: `1px solid ${colors.border}`, background: colors.card, color: colors.text, fontFamily: "monospace", fontSize: 14, boxSizing: "border-box" }}
          placeholder='例如: safe_inv(pe_ttm, 0, 1000) - roc(c_m, 60)'
        />
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 8 }}>
          <label style={{ fontSize: 12 }}>名称<input value={name} onChange={(e) => setName(e.target.value)} style={inputStyle(colors)} /></label>
          <label style={{ fontSize: 12 }}>开始<input type="date" value={start} onChange={(e) => setStart(e.target.value)} style={inputStyle(colors)} /></label>
          <label style={{ fontSize: 12 }}>结束<input type="date" value={end} onChange={(e) => setEnd(e.target.value)} style={inputStyle(colors)} /></label>
          <label style={{ fontSize: 12 }}>分组数
            <select value={groups} onChange={(e) => setGroups(Number(e.target.value))} style={inputStyle(colors)}>
              {[3, 5, 8, 10].map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
          </label>
          <label style={{ fontSize: 12 }}>未来收益(日)
            <select value={forward} onChange={(e) => setForward(Number(e.target.value))} style={inputStyle(colors)}>
              {[5, 10, 20, 30, 60].map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </label>
          <button onClick={doValidate} style={btnStyle(colors, "#f0a020")}>校验</button>
          <button onClick={runMine} disabled={running} style={btnStyle(colors, colors.accent)}>{running ? "挖掘中…" : "开始挖掘"}</button>
        </div>
        {valid && (
          <div style={{ fontSize: 12, marginTop: 8, color: valid.ok ? colors.down : colors.up }}>
            {valid.ok ? `✓ 表达式有效（试算值 ${valid.sample_value ?? "—"}）` : `✗ ${valid.error}`}
          </div>
        )}
        {error && <div style={{ color: colors.up, marginTop: 6, fontSize: 13 }}>{error}</div>}
        <div style={{ marginTop: 6 }}>
          <button onClick={() => setShowFns(!showFns)} style={{ fontSize: 12, color: colors.accent, background: "none", border: 0, cursor: "pointer", padding: 0 }}>
            {showFns ? "▲ 收起" : "▼ 函数/变量参考"}
          </button>
        </div>
        {showFns && (
          <div style={{ marginTop: 8, display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))", gap: 10, fontSize: 12 }}>
            {Object.entries(fns).map(([cat, items]) => (
              <div key={cat} style={{ background: colors.tableStripe, borderRadius: 6, padding: 8 }}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>{cat}</div>
                {Object.entries(items).map(([k, v]) => (
                  <div key={k} style={{ marginBottom: 2 }}><code style={{ color: colors.accent }}>{k}</code> — {v}</div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 报告 */}
      {report && <Report report={report} colors={colors} />}

      {/* 历史 */}
      {history.length > 0 && (
        <div style={{ marginTop: 16, background: colors.card, borderRadius: 10, padding: 12, border: `1px solid ${colors.border}` }}>
          <div style={{ fontWeight: 700, marginBottom: 8, fontSize: 14 }}>挖掘历史</div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
            <thead><tr style={{ color: colors.muted, textAlign: "left" }}>
              <th style={{ padding: "5px 8px" }}>#</th><th>名称</th><th>表达式</th><th>IC</th><th>ICIR</th><th>评级</th><th></th>
            </tr></thead>
            <tbody>
              {history.map((h) => (
                <tr key={h.id} style={{ borderTop: `1px solid ${colors.border}` }}>
                  <td style={{ padding: "5px 8px" }}>{h.id}</td>
                  <td><a style={{ color: colors.accent, cursor: "pointer" }} onClick={() => loadDetail(h.id)}>{h.name}</a></td>
                  <td style={{ fontFamily: "monospace", fontSize: 11.5, maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{h.expr}</td>
                  <td>{h.ic_mean?.toFixed(4) ?? "—"}</td>
                  <td>{h.icir?.toFixed(3) ?? "—"}</td>
                  <td style={{ color: ratingColor(h.rating, colors), fontWeight: 600 }}>{h.rating}</td>
                  <td><button onClick={() => del(h.id)} style={{ fontSize: 11, color: colors.up, background: "none", border: 0, cursor: "pointer" }}>删</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Report({ report, colors }: { report: FactorMineReport; colors: { card: string; border: string; up: string; down: string; muted: string; text: string; accent: string; tableStripe: string } }) {
  const rc = ratingColor(report.rating, colors);
  const icOption = {
    tooltip: { trigger: "axis" },
    grid: { left: 46, right: 16, top: 30, bottom: 36 },
    xAxis: { type: "category", data: report.ic_series.map((p) => p.date), axisLabel: { fontSize: 10 } },
    yAxis: { type: "value" },
    series: [
      { name: "IC", type: "line", data: report.ic_series.map((p) => p.ic), showSymbol: false, lineStyle: { color: colors.accent, width: 1.5 }, areaStyle: { opacity: 0.08 } },
      { name: "均值", type: "line", data: report.ic_series.map(() => report.ic_mean), showSymbol: false, lineStyle: { color: colors.muted, type: "dashed", width: 1 } },
    ],
  };
  const gKeys = Object.keys(report.group_means).map(Number).sort((a, b) => a - b);
  const groupOption = {
    tooltip: {},
    grid: { left: 46, right: 16, top: 20, bottom: 36 },
    xAxis: { type: "category", data: gKeys.map((g) => `组${g}`) },
    yAxis: { type: "value" },
    series: [{
      type: "bar",
      data: gKeys.map((g) => ({ value: report.group_means[g], itemStyle: { color: report.group_means[g] >= 0 ? colors.up : colors.down, opacity: 0.85 } })),
      barWidth: "45%",
    }],
  };
  const lsOption = {
    tooltip: { trigger: "axis" },
    grid: { left: 46, right: 16, top: 30, bottom: 36 },
    xAxis: { type: "category", data: report.long_short.map((p) => p[0]), axisLabel: { fontSize: 10 } },
    yAxis: { type: "value" },
    series: [{ name: "多空累计", type: "line", data: report.long_short.map((p) => p[1]), showSymbol: false, lineStyle: { color: "#8e5cf0", width: 1.5 } }],
  };

  return (
    <div style={{ background: colors.card, borderRadius: 10, padding: 14, border: `1px solid ${colors.border}` }}>
      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
        <span style={{ fontSize: 15, fontWeight: 700 }}>{report.name}</span>
        <span style={{ padding: "2px 10px", borderRadius: 999, fontSize: 12, fontWeight: 600, color: "#fff", background: rc }}>{report.rating}</span>
        <span style={{ fontSize: 12, color: colors.muted }}>{report.n_periods} 期截面 · {report.n_stocks} 只 · 未来 {report.forward_days} 日</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(130px,1fr))", gap: 10, marginBottom: 14 }}>
        <Metric label="IC 均值" value={report.ic_mean.toFixed(4)} colors={colors} />
        <Metric label="ICIR" value={report.icir.toFixed(3)} colors={colors} />
        <Metric label="t 值" value={report.t_stat.toFixed(2)} colors={colors} />
        <Metric label="IC 胜率" value={`${(report.ic_win_rate * 100).toFixed(0)}%`} colors={colors} />
        <Metric label="多空单调差" value={report.monotonic_spread.toFixed(4)} colors={colors} />
        <Metric label="单调评分" value={report.mono_score.toFixed(2)} colors={colors} />
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(300px,1fr))", gap: 12 }}>
        <EChart option={icOption as never} height={220} />
        <EChart option={groupOption as never} height={220} />
      </div>
      <div style={{ marginTop: 12 }}>
        <EChart option={lsOption as never} height={200} />
      </div>
      {Object.keys(report.corr_with_existing || {}).length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
            与现有因子相关性（最大 |{report.max_abs_corr}| {report.max_abs_corr > 0.7 ? "⚠ 高度重复" : report.max_abs_corr > 0.5 ? "⚠ 中度重复" : "✓ 独立性良好"}）
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {Object.entries(report.corr_with_existing).map(([k, v]) => (
              <span key={k} style={{ fontSize: 11.5, padding: "3px 9px", borderRadius: 5, background: colors.tableStripe, border: `1px solid ${colors.border}` }}>
                {k} <b style={{ color: Math.abs(v) > 0.5 ? colors.up : colors.text }}>{v.toFixed(2)}</b>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ratingColor(r: string, colors: { up: string; down: string; accent: string }): string {
  if (r === "优秀") return "#3ba272";
  if (r === "可用") return colors.accent;
  if (r === "弱") return colors.down;
  return colors.up;
}

const inputStyle = (c: { text: string; card: string; border: string }) => ({
  padding: "4px 8px",
  borderRadius: 5,
  border: `1px solid ${c.border}`,
  background: c.card,
  color: c.text,
  marginLeft: 4,
});

const btnStyle = (c: { text: string; card: string; border: string }, bg: string) => ({
  padding: "6px 16px",
  borderRadius: 6,
  border: 0,
  background: bg,
  color: "#fff",
  cursor: "pointer",
  fontSize: 13,
});

function Metric({ label, value, colors }: { label: string; value: string; colors: { card: string; muted: string; text: string; border: string } }) {
  return (
    <div style={{ background: colors.card, borderRadius: 8, padding: "8px 12px", border: `1px solid ${colors.border}` }}>
      <div style={{ fontSize: 11.5, color: colors.muted }}>{label}</div>
      <div style={{ fontSize: 17, fontWeight: 700 }}>{value}</div>
    </div>
  );
}
