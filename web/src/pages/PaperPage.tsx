import { useCallback, useEffect, useState, type CSSProperties } from "react";
import EChart from "../components/EChart";
import { api, CostProfile, PaperDetail, PaperTask, StrategyInfo } from "../api/client";
import { useTheme } from "../theme";
import { PageHeader } from "../components/ui";
import EvalBadges from "../components/EvalBadges";
import { signalLabel } from "../signalLabel";

/** W47-1：创建前本地预览徽章（与 cost_defaults / 回测默认同契约） */
const DEFAULT_COST_PROFILE: CostProfile = {
  commission: 0.00025,
  slippage: 0.0001,
  stamp_tax: 0.0005,
  min_commission: 5.0,
  risk_free_rate: 0.025,
  source: "default",
  label: "万2.5+万1滑点+卖出万5印花税",
};

/** 产品定稿口径句（勿改字） */
const COST_DISCLAIMER = "与回测默认成本同契约，非券商实盘";


/** 模拟盘（任务列表 + 创建 + 详情：净值曲线/成交）。 */
export default function PaperPage() {
  const [tasks, setTasks] = useState<PaperTask[]>([]);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [detail, setDetail] = useState<PaperDetail | null>(null);
  const [createBadges, setCreateBadges] = useState<{
    cost_profile?: import("../api/client").CostProfile | null;
    valuation_pit?: import("../api/client").ValuationPit | null;
    pead_pit?: import("../api/client").PeadPit | null;
  } | null>(null);
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
      const single = s.filter((x) => !(x.multi_asset || x.default_params?.multi_asset));
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
    const meta = strategies.find((s) => s.key === skey);
    const isPortfolio = Boolean(meta?.multi_asset || meta?.default_params?.multi_asset);
    // W47-1 §5.3：创建前前端校验（原文勿改）
    if (!strategies.length || !skey) {
      setError("请先选择策略（策略列表加载中或尚未选择）");
      return;
    }
    if (!isPortfolio && !symbols.trim()) {
      setError("请填写标的代码，例如 sh600519");
      return;
    }
    if (isPortfolio && !indexCode.trim()) {
      setError("请填写指数代码，例如 000906");
      return;
    }
    if (!Number.isFinite(cash) || cash <= 0) {
      setError("初始资金须为正数");
      return;
    }
    try {
      const task = await api.paperCreate({
        name: name || `${meta?.name || skey} 模拟`,
        strategy_key: skey,
        kind: isPortfolio ? "portfolio" : "single",
        symbols: isPortfolio ? "" : symbols,
        index_code: isPortfolio ? indexCode : "",
        initial_cash: cash,
        params_json: "{}",
      });
      setCreateBadges({
        cost_profile: task.cost_profile,
        valuation_pit: task.valuation_pit,
        pead_pit: task.pead_pit,
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
  const isPortfolio = Boolean(meta?.multi_asset || meta?.default_params?.multi_asset);

  return (
    <div style={{ maxWidth: 1280, margin: "0 auto" }}>
      <PageHeader
        title="模拟盘"
        desc="手动建任务：自选策略与标的，实时价撮合 · 日频调仓 · 风险截断。与「信号中心 → 镜像组合」的区别：那边完全跟随每日选股信号自动进出，无需任何配置"
      />
      {/* 创建表单 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 10, marginBottom: 12 }}>
        <label>任务名<input value={name} onChange={(e) => setName(e.target.value)} placeholder="自动命名" style={inputStyle(colors)} /></label>
        <label>
          策略
          <select value={skey} onChange={(e) => setSkey(e.target.value)} style={inputStyle(colors)}>
            {strategies.filter((s) => !(s.multi_asset || s.default_params?.multi_asset)).map((s) => (
              <option key={s.key} value={s.key}>{s.name}</option>
            ))}
            {strategies.filter((s) => s.multi_asset || s.default_params?.multi_asset).map((s) => (
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
      {/* W47-1：创建前默认成本徽章 + 口径句 */}
      <div style={{ margin: "8px 0 12px", display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
        <EvalBadges cost_profile={DEFAULT_COST_PROFILE} colors={colors} />
        <span style={{ fontSize: 12, color: colors.muted }}>{COST_DISCLAIMER}</span>
      </div>
      <button onClick={create} style={btnStyle}>创建并运行</button>
      <button onClick={refresh} style={{ ...btnStyle, background: colors.muted }}>刷新</button>
      {createBadges && (
        <div style={{ margin: "10px 0" }}>
          <div style={{ fontSize: 12, color: colors.muted, marginBottom: 6 }}>
            已按回测默认成本建账 · {COST_DISCLAIMER}
          </div>
          <EvalBadges
            cost_profile={createBadges.cost_profile}
            valuation_pit={createBadges.valuation_pit}
            pead_pit={createBadges.pead_pit}
            colors={colors}
          />
        </div>
      )}
      {error && <div style={{ color: colors.up, margin: "8px 0" }}>{error}</div>}
      {progressHint && !error && <div style={{ color: colors.muted, margin: "8px 0", fontSize: 13 }}>{progressHint}</div>}

      {/* 任务列表：表格（2026-11-09 老刘要求：卡片换成列表更直观）
          卡片一屏只放得下 3~4 个，且各字段高度不齐、横向无法对比；
          表格能一屏看完所有任务，净值/收益按列对齐。 */}
      <div style={{
        marginTop: 16, background: colors.card, borderRadius: 10,
        border: `1px solid ${colors.border}`, overflow: "hidden",
      }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
          <thead>
            <tr style={{ background: colors.bg, color: colors.muted, fontSize: 11.5 }}>
              {["#", "任务", "策略", "标的", "净值", "收益", "持仓", "状态", "最近运行", "操作"].map((h, i) => (
                <th key={h} style={{
                  textAlign: i >= 4 && i <= 6 ? "right" : "left",
                  padding: "8px 10px", fontWeight: 600, whiteSpace: "nowrap",
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.id}
                onClick={() => openDetail(t.id)}
                style={{ borderTop: `1px solid ${colors.border}`, cursor: "pointer" }}
                onMouseEnter={(e) => (e.currentTarget.style.background = colors.bg)}
                onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
              >
                <td style={{ padding: "8px 10px", color: colors.muted }}>{t.id}</td>
                <td style={{ padding: "8px 10px", fontWeight: 600 }}>
                  {t.name}
                  {t.error_msg && (
                    <div style={{ fontWeight: 400, fontSize: 11, color: colors.up, marginTop: 2 }}>
                      {t.error_msg}
                    </div>
                  )}
                </td>
                <td style={{ padding: "8px 10px", color: colors.muted }}>{t.strategy_key}</td>
                <td style={{ padding: "8px 10px", color: colors.muted }}>
                  {t.kind === "portfolio" ? `指数 ${t.index_code}` : t.symbols}
                </td>
                <td style={{ padding: "8px 10px", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                  ¥{t.equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                </td>
                <td style={{
                  padding: "8px 10px", textAlign: "right", fontWeight: 600,
                  fontVariantNumeric: "tabular-nums",
                  color: t.pnl_pct >= 0 ? colors.up : colors.down,
                }}>
                  {t.pnl_pct >= 0 ? "+" : ""}{(t.pnl_pct * 100).toFixed(2)}%
                </td>
                <td style={{ padding: "8px 10px", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                  {t.positions_count}
                </td>
                <td style={{ padding: "8px 10px" }}>
                  <span style={{ color: t.enabled ? "#1e8449" : colors.muted }}>
                    {t.enabled ? "● 自动" : "○ 暂停"}
                  </span>
                </td>
                <td style={{ padding: "8px 10px", color: colors.muted, whiteSpace: "nowrap" }}>
                  {t.last_run_at?.slice(0, 16) || "未运行"}
                </td>
                <td style={{ padding: "8px 10px", whiteSpace: "nowrap" }}>
                  <button onClick={(e) => { e.stopPropagation(); toggle(t.id); }} style={smallBtn(colors)}>
                    {t.enabled ? "暂停" : "启用"}
                  </button>
                  <button onClick={(e) => { e.stopPropagation(); runNow(t.id); }} style={smallBtn(colors)}
                    disabled={runningId === t.id}>
                    {runningId === t.id ? "运行中…" : "立即运行"}
                  </button>
                  <button onClick={(e) => { e.stopPropagation(); remove(t.id); }}
                    style={{ ...smallBtn(colors), color: colors.up }}>删除</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {tasks.length === 0 && (
          <div style={{ padding: 24, textAlign: "center", color: colors.muted, fontSize: 13 }}>
            还没有模拟盘任务 ——「启用」状态的策略会自动建（见下方说明）
          </div>
        )}
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
  // 持仓按「纯数字代码」归并：后端偶尔混用 sh600519 / 600519 / 600519.SH，
  // 直接渲染会出现同一只股票显示两行的「重复持仓」。
  const positions = (() => {
    const norm = (s: string) => s.replace(/^(sh|sz|bj)/i, "").replace(/\.(SH|SS|SZ|BJ)$/i, "");
    const merged = new Map<string, { code: string; shares: number; cost: number }>();
    for (const [sym, raw] of Object.entries(detail.positions || {})) {
      const p = (raw || {}) as { shares?: number; cost?: number };
      const shares = Number(p.shares || 0);
      const cost = Number(p.cost || 0);
      const key = norm(sym);
      const prev = merged.get(key);
      if (!prev) {
        merged.set(key, { code: sym, shares, cost });
      } else {
        const total = prev.shares + shares;
        prev.cost = total > 0 ? (prev.cost * prev.shares + cost * shares) / total : prev.cost;
        prev.shares = total;
      }
    }
    return [...merged.values()].filter((p) => p.shares > 0);
  })();
  const tbl: CSSProperties = { width: "100%", borderCollapse: "collapse", fontSize: 12.5, whiteSpace: "nowrap" };
  const scrollX: CSSProperties = { overflowX: "auto" };
  const costProfile = detail.cost_profile || (
    detail.commission != null && detail.slippage != null
      ? {
          commission: detail.commission,
          slippage: detail.slippage,
          stamp_tax: 0.0005,
          source: "default",
          label: "万2.5+万1滑点+卖出万5印花税",
        }
      : DEFAULT_COST_PROFILE
  );
  const commissionRate = detail.commission ?? costProfile.commission ?? 0.00025;
  const slippageRate = detail.slippage ?? costProfile.slippage ?? 0.0001;
  const costMetric = `万${(commissionRate * 10000).toFixed(1)} / 万${(slippageRate * 10000).toFixed(1)}`;

  return (
    <div style={{ marginTop: 20, background: colors.card, borderRadius: 10, padding: 16, border: `1px solid ${colors.border}` }}>
      <div style={{ fontWeight: 700, marginBottom: 8 }}>模拟盘 #{detail.id} 详情</div>
      <div style={{ marginBottom: 12, display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
        <EvalBadges cost_profile={costProfile} colors={colors} />
        <span style={{ fontSize: 12, color: colors.muted }}>{COST_DISCLAIMER}</span>
      </div>
      <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 12 }}>
        <Metric label="当前净值" colors={colors} value={`¥${detail.equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}`} />
        <Metric label="收益率" colors={colors} value={`${((detail.pnl_pct || 0) * 100).toFixed(2)}%`} />
        <Metric label="成交笔数" colors={colors} value={`${trades.length}`} />
        <Metric label="成本口径" colors={colors} value={costMetric} />
        <Metric label="佣金" colors={colors} value={`万${(commissionRate * 10000).toFixed(1)}`} />
        <Metric label="滑点" colors={colors} value={`万${(slippageRate * 10000).toFixed(1)}`} />
        {typeof detail.fees_paid_commission === "number" && (
          <Metric
            label="累计佣金"
            colors={colors}
            value={`¥${detail.fees_paid_commission.toLocaleString(undefined, { maximumFractionDigits: 2 })}`}
          />
        )}
        {limits && Object.keys(limits).length > 0 && (
          <Metric label="风险截断" colors={colors} value={`${(detail.risk_clamps || []).length} 次`} />
        )}
      </div>
      {typeof detail.fees_paid_commission === "number" && (
        <div style={{ fontSize: 12, color: colors.muted, marginTop: -6, marginBottom: 10 }}>不含印花税分项</div>
      )}
      {curve.length > 0 && <EChart option={option as never} height={340} />}
      <div style={{ marginTop: 14 }}>
        <h4 style={{ margin: "0 0 8px", fontSize: 14 }}>当前持仓（{positions.length} 只）</h4>
        <div style={scrollX}>
          <table style={tbl}>
            <thead><tr style={{ color: colors.muted, textAlign: "left" }}>
              <th style={{ padding: "5px 8px" }}>标的</th><th>股数</th><th>成本价</th><th>成本市值</th>
            </tr></thead>
            <tbody>
              {positions.length ? positions.map((p) => (
                <tr key={p.code} style={{ borderTop: `1px solid ${colors.border}` }}>
                  <td style={{ padding: "5px 8px" }}>{p.code}</td>
                  <td>{Math.round(p.shares).toLocaleString()}</td>
                  <td>{p.cost.toFixed(2)}</td>
                  <td>{Math.round(p.shares * p.cost).toLocaleString()}</td>
                </tr>
              )) : (
                <tr><td colSpan={4} style={{ padding: "5px 8px", color: colors.muted }}>空仓</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      {trades.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <h4 style={{ margin: "0 0 8px", fontSize: 14 }}>最近成交（共 {trades.length} 笔）</h4>
          <div style={scrollX}>
            <table style={tbl}>
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
                    <td>{signalLabel(t.signal_type)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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
