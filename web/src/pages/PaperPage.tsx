import { useCallback, useEffect, useState } from "react";
import EChart from "../components/EChart";
import { api, PaperDetail, PaperTask, StrategyInfo } from "../api/client";
import { useTheme } from "../theme";

/** 模拟盘（任务列表 + 创建 + 详情：净值曲线/成交）。 */
export default function PaperPage() {
  const [tasks, setTasks] = useState<PaperTask[]>([]);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [detail, setDetail] = useState<PaperDetail | null>(null);
  const [error, setError] = useState("");
  const { colors } = useTheme();

  // 创建表单
  const [name, setName] = useState("");
  const [skey, setSkey] = useState("");
  const [symbols, setSymbols] = useState("sh600519");
  const [indexCode, setIndexCode] = useState("000906");
  const [cash, setCash] = useState(1000000);

  const refresh = useCallback(() => {
    api.paperTasks().then(setTasks).catch((e) => setError((e as Error).message));
  }, []);

  useEffect(() => {
    refresh();
    api.strategies().then((s) => {
      setStrategies(s);
      const single = s.filter((x) => !x.default_params?.multi_asset);
      if (single.length) setSkey(single[0].key);
    });
  }, [refresh]);

  const openDetail = async (id: number) => {
    try {
      const d = await api.paperDetail(id);
      setDetail(d);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const create = async () => {
    setError("");
    try {
      const meta = strategies.find((s) => s.key === skey);
      const isPortfolio = Boolean(meta?.default_params?.multi_asset);
      const task = await api.paperCreate({
        name: name || `${meta?.name || skey} 模拟`,
        strategy_key: skey,
        kind: isPortfolio ? "portfolio" : "single",
        symbols: isPortfolio ? "" : symbols,
        index_code: isPortfolio ? indexCode : "",
        initial_cash: cash,
        params_json: "{}",
      });
      await api.paperRun(task.id);
      refresh();
      openDetail(task.id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const toggle = async (id: number) => {
    try {
      await api.paperToggle(id);
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // 「立即运行」：带反馈（禁用+提示），避免后台任务无感知
  const [runningId, setRunningId] = useState<number | null>(null);
  const runNow = async (id: number) => {
    if (runningId !== null) return;
    setRunningId(id);
    setError("");
    try {
      await api.paperRun(id);
      setProgressHint(`任务 #${id} 已触发，运行中…`);
      // 后台线程执行，延时刷新两次以展示结果
      setTimeout(() => { refresh(); setProgressHint(`任务 #${id} 运行完成，已刷新`); }, 4000);
    } catch (e) {
      setError((e as Error).message);
      setProgressHint("");
    } finally {
      setRunningId(null);
    }
  };

  const [progressHint, setProgressHint] = useState("");

  const remove = async (id: number) => {
    if (!window.confirm(`删除模拟盘任务 #${id}？`)) return;
    try {
      await api.paperDelete(id);
      if (detail?.id === id) setDetail(null);
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const meta = strategies.find((s) => s.key === skey);
  const isPortfolio = Boolean(meta?.default_params?.multi_asset);

  return (
    <div style={{ padding: 16 }}>
      {/* 创建表单 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 10, marginBottom: 12 }}>
        <label>任务名<input value={name} onChange={(e) => setName(e.target.value)} placeholder="自动命名" style={inputStyle(colors)} /></label>
        <label>
          策略
          <select value={skey} onChange={(e) => setSkey(e.target.value)} style={inputStyle(colors)}>
            {strategies.filter((s) => !s.default_params?.multi_asset).map((s) => (
              <option key={s.key} value={s.key}>{s.name}</option>
            ))}
            {strategies.filter((s) => s.default_params?.multi_asset).map((s) => (
              <option key={s.key} value={s.key}>{s.name}（组合）</option>
            ))}
          </select>
        </label>
        {isPortfolio ? (
          <label>指数代码<input value={indexCode} onChange={(e) => setIndexCode(e.target.value)} style={inputStyle(colors)} /></label>
        ) : (
          <label>标的<input value={symbols} onChange={(e) => setSymbols(e.target.value)} style={inputStyle(colors)} /></label>
        )}
        <label>初始资金<input type="number" value={cash} onChange={(e) => setCash(Number(e.target.value))} style={inputStyle(colors)} /></label>
      </div>
      <button onClick={create} style={btnStyle}>创建并运行</button>
      <button onClick={refresh} style={{ ...btnStyle, background: colors.muted }}>刷新</button>
      {error && <div style={{ color: colors.up, margin: "8px 0" }}>{error}</div>}
      {progressHint && !error && <div style={{ color: colors.muted, margin: "8px 0", fontSize: 13 }}>{progressHint}</div>}

      {/* 任务列表 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(260px,1fr))", gap: 12, marginTop: 16 }}>
        {tasks.map((t) => (
          <div key={t.id} onClick={() => openDetail(t.id)} style={{ background: colors.card, borderRadius: 10, padding: 14, cursor: "pointer", border: `1px solid ${colors.border}` }}>
            <div style={{ fontWeight: 600 }}>#{t.id} {t.name}</div>
            <div style={{ fontSize: 12, color: colors.muted, margin: "4px 0" }}>
              {t.strategy_key} · {t.kind === "portfolio" ? `指数 ${t.index_code}` : t.symbols}
            </div>
            <div style={{ fontSize: 20, fontWeight: 700, color: t.pnl_pct >= 0 ? colors.up : colors.down }}>
              ¥{t.equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}
              <span style={{ fontSize: 13, marginLeft: 8 }}>{t.pnl_pct >= 0 ? "+" : ""}{(t.pnl_pct * 100).toFixed(2)}%</span>
            </div>
            <div style={{ fontSize: 12, color: colors.muted }}>
              持仓 {t.positions_count} · {t.enabled ? "🟢 自动" : "⚪ 暂停"} · {t.last_run_at?.slice(0, 16) || "未运行"}
            </div>
            {t.error_msg && <div style={{ fontSize: 12, color: colors.up }}>{t.error_msg}</div>}
            <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
              <button onClick={(e) => { e.stopPropagation(); toggle(t.id); }} style={smallBtn(colors)}>{t.enabled ? "暂停" : "启用"}</button>
              <button onClick={(e) => { e.stopPropagation(); runNow(t.id); }} style={smallBtn(colors)} disabled={runningId === t.id}>
                {runningId === t.id ? "运行中…" : "立即运行"}
              </button>
              <button onClick={(e) => { e.stopPropagation(); remove(t.id); }} style={{ ...smallBtn(colors), color: colors.up }}>删除</button>
            </div>
          </div>
        ))}
      </div>

      {/* 详情 */}
      {detail && <DetailPanel detail={detail} />}
    </div>
  );
}

function DetailPanel({ detail }: { detail: PaperDetail }) {
  const { colors } = useTheme();
  const curve = detail.curve || [];
  const option = {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { data: ["账户净值"], top: 0 },
    grid: { left: 60, right: 20, top: 40, bottom: 40 },
    xAxis: { type: "category", data: curve.map((p) => p.date) },
    yAxis: { type: "value", scale: true },
    series: [{
      name: "账户净值",
      type: "line",
      data: curve.map((p) => p.equity),
      showSymbol: false,
      lineStyle: { color: "#cf1322", width: 1.5 },
      areaStyle: { opacity: 0.06 },
    }],
  };
  const trades = detail.trades || [];
  const limits = detail.risk_limits;
  return (
    <div style={{ marginTop: 20, background: colors.card, borderRadius: 10, padding: 16, border: `1px solid ${colors.border}` }}>
      <div style={{ fontWeight: 700, marginBottom: 12 }}>模拟盘 #{detail.id} 详情</div>
      <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 12 }}>
        <Metric label="当前净值" colors={colors} value={`¥${detail.equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}`} />
        <Metric label="收益率" colors={colors} value={`${((detail.pnl_pct || 0) * 100).toFixed(2)}%`} />
        <Metric label="成交笔数" colors={colors} value={`${trades.length}`} />
        {limits && Object.keys(limits).length > 0 && (
          <Metric label="风险截断" colors={colors} value={`${(detail.risk_clamps || []).length} 次`} />
        )}
      </div>
      {curve.length > 0 && <EChart option={option as never} height={340} />}
      {trades.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <h4 style={{ margin: "0 0 8px", fontSize: 14 }}>最近成交</h4>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
            <thead><tr style={{ color: colors.muted, textAlign: "left" }}>
              <th style={{ padding: "5px 8px" }}>日期</th><th>标的</th><th>方向</th><th>价格</th><th>数量</th><th>信号</th>
            </tr></thead>
            <tbody>
              {trades.slice(-15).reverse().map((t, i) => (
                <tr key={i} style={{ borderTop: `1px solid ${colors.border}` }}>
                  <td style={{ padding: "5px 8px" }}>{(t as { date?: string }).date ? (t as { date?: string }).date : t.trade_date}</td>
                  <td>{t.symbol}</td>
                  <td style={{ color: t.side === "BUY" ? colors.up : colors.down }}>{t.side === "BUY" ? "买入" : "卖出"}</td>
                  <td>{t.price}</td>
                  <td>{t.shares}</td>
                  <td>{t.signal_type || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {detail.error_msg && <div style={{ color: colors.up, marginTop: 8 }}>{detail.error_msg}</div>}
    </div>
  );
}

const inputStyle = (c: { text: string; card: string; border: string }) => ({
  width: "100%",
  padding: "6px 10px",
  borderRadius: 6,
  border: `1px solid ${c.border}`,
  background: c.card,
  color: c.text,
  marginTop: 4,
  boxSizing: "border-box" as const,
});

const btnStyle = {
  padding: "8px 22px",
  borderRadius: 6,
  border: 0,
  background: "#1668dc",
  color: "#fff",
  cursor: "pointer",
  marginRight: 8,
};

const smallBtn = (c: { card: string; border: string }) => ({
  padding: "3px 10px",
  borderRadius: 4,
  border: `1px solid ${c.border}`,
  background: c.card,
  fontSize: 12,
  cursor: "pointer",
});

function Metric({ label, value, colors }: { label: string; value: string; colors: { card: string; text: string; muted: string; border: string } }) {
  return (
    <div style={{ background: colors.card, borderRadius: 8, padding: "10px 16px", minWidth: 110, border: `1px solid ${colors.border}` }}>
      <div style={{ fontSize: 12, color: colors.muted }}>{label}</div>
      <div style={{ fontSize: 17, fontWeight: 600 }}>{value}</div>
    </div>
  );
}
