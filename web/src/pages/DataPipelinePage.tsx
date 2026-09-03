import { useCallback, useEffect, useState } from "react";
import { api, AssetsReport, AssetItem, CoverageBlock, HealthRuleRow, HealthReport, LineageReport } from "../api/client";
import { Badge, Card, PageHeader } from "../components/ui";
import { useTheme, ThemeColors } from "../theme";

/** 数据管道页：数据资产 → 源头 → 血缘 → 处理步骤 → 任务执行 → 健康度。 */

type Status = "ok" | "warn" | "stale" | "empty" | "RUNNING";

const STATUS_META: Record<string, { text: string; short: string }> = {
  ok: { text: "已更新至最新交易日", short: "最新" },
  warn: { text: "滞后", short: "滞后" },
  stale: { text: "已过期", short: "过期" },
  empty: { text: "无数据", short: "空" },
};

function statusColor(s: string, c: ThemeColors): string {
  if (s === "ok" || s === "OK" || s === "SUCCESS") return c.down;
  if (s === "empty" || s === "FAIL" || s === "FAILED") return c.up;
  if (s === "warn" || s === "RUNNING") return c.accent;
  return c.muted;
}

const fmtRows = (n: number) =>
  n >= 10000 ? `${(n / 10000).toFixed(1)} 万` : n.toLocaleString();
const fmtDate = (d: string | null | undefined) => (d ? d.slice(0, 10) : "—");

/** 管道步骤的中文说明：做什么 → 产出落到哪 */
const STEP_DESC: Record<string, string> = {
  extract_index_kline: "拉取 8 个核心指数日K（增量）→ index_kline_daily",
  extract_stock_kline: "核心池个股前复权日K（增量）→ kline_daily + Bronze 快照",
  extract_attributes: "全市场截面属性 daily_basic → stocks（估值/市值/行业）",
  clean_bars: "K线清洗：去重、异常值剔除 → 输出质检报告",
  extract_eastmoney_news: "东财全市场新闻流 → Bronze/text + 市场情绪",
  extract_announcements: "上市公司公告（按 codes 精确关联，去重）→ 个股情绪",
  extract_wechat_articles: "公众号财经语料 → Bronze/text",
  clean_text: "文本清洗：分段、匹配个股提及 → Silver",
  score_sentiment: "情绪打分（多空词典 / LLM）→ 市场与个股情绪日表",
  compute_mined_factors: "按因子注册表计算 GP 挖掘因子 → factor_mined_daily",
  compute_factors: "截面基础因子计算（14 因子）→ factor_daily",
  sync_duckdb: "同步 SQLite → DuckDB 分析库（回测/研究读取）",
};

export default function DataPipelinePage() {
  const { colors } = useTheme();
  const [lin, setLin] = useState<LineageReport | null>(null);
  const [health, setHealth] = useState<HealthReport | null>(null);
  const [assets, setAssets] = useState<AssetsReport | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [rules, setRules] = useState<HealthRuleRow[]>([]);
  const [rulesOpen, setRulesOpen] = useState(false);
  const [editing, setEditing] = useState<number | null>(null);
  const [editTh, setEditTh] = useState("");
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({
    "行情": true, "因子": true, "文本": true, "元数据": false,
  });

  const refresh = useCallback(async () => {
    try {
      const [l, h, a] = await Promise.all([
        api.monitorLineage(),
        api.monitorHealth().catch(() => null),
        api.monitorAssets().catch(() => null),
      ]);
      setLin(l);
      if (h) setHealth(h);
      if (a) setAssets(a);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
    api.healthRules().then(setRules).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, [refresh]);

  if (!lin) {
    return <div style={{ padding: 24, color: colors.muted }}>加载数据管道视图…{error}</div>;
  }

  const sum = assets?.summary;

  return (
    <div style={{ maxWidth: 1320, margin: "0 auto" }}>
      <PageHeader
        title="数据管道"
        desc="数据资产 · 源头 · 血缘 · 处理步骤 · 任务执行 · 健康度（30s 自动刷新）"
        actions={
          <>
            <button
              onClick={async () => {
                setBusy(true);
                try {
                  const r = await fetch("/api/v1/monitor/pipeline/run", { method: "POST" }).then((x) => x.json());
                  if (!r.ok) window.alert(r.error || "已有任务在运行");
                  else setTimeout(refresh, 2500);
                } finally {
                  setBusy(false);
                }
              }}
              disabled={busy}
              style={{
                padding: "6px 14px", borderRadius: 7, border: `1px solid ${colors.accent}`,
                background: `${colors.accent}14`, color: colors.accent,
                cursor: busy ? "wait" : "pointer", fontSize: 12.5,
              }}
            >
              ▶ 立即运行
            </button>
            <span style={{ fontSize: 11.5, color: colors.muted }}>
              最近运行 #{lin.last_run?.run_id} · {lin.last_run?.status} ·{" "}
              {lin.last_run?.started_at?.slice(0, 16).replace("T", " ")}
            </span>
          </>
        }
      />

      {/* —— ⓿ 结论条：数据到底新不新鲜 —— */}
      {sum && (
        <div
          style={{
            padding: "12px 16px", borderRadius: 10, marginBottom: 14,
            background: sum.verdict_level === "ok" ? `${colors.down}10` : sum.verdict_level === "warn" ? `${colors.accent}10` : `${colors.up}10`,
            border: `1px solid ${sum.verdict_level === "ok" ? colors.down : sum.verdict_level === "warn" ? colors.accent : colors.up}55`,
          }}
        >
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 14 }}>
            <span style={{ fontSize: 15 }}>{sum.verdict_level === "ok" ? "✅" : sum.verdict_level === "warn" ? "⚠️" : "🔴"}</span>
            <b style={{ fontSize: 14, color: statusColor(sum.verdict_level, colors) }}>{sum.verdict}</b>
            <span style={{ fontSize: 12.5, color: colors.muted }}>
              行情最新交易日 <b className="num" style={{ color: colors.text }}>{fmtDate(sum.latest_trade_date)}</b>
              {sum.lag_trading_days !== null && (
                <>
                  {" "}（滞后 <b className="num" style={{ color: statusColor(sum.verdict_level, colors) }}>{sum.lag_trading_days}</b> 个交易日
                  / {sum.lag_calendar_days} 个自然日）
                </>
              )}
            </span>
            {sum.pending_today && (
              <span style={{ fontSize: 12, color: colors.muted }}>
                ⏳ 今日（{assets?.today?.slice(5)}）尚未收盘，日线数据 16:00 后发布 —— 不计入滞后
              </span>
            )}
            <span style={{ fontSize: 12.5, color: colors.muted }}>
              覆盖 <b className="num" style={{ color: colors.text }}>{sum.symbols?.toLocaleString() ?? "—"}</b> 只标的 ·
              共 <b className="num" style={{ color: colors.text }}>{fmtRows(sum.total_rows)}</b> 行
            </span>
            {sum.worst && (
              <span style={{ fontSize: 12, color: colors.up }}>
                最滞后：{sum.worst.label}（{fmtDate(sum.worst.latest)}，滞后 {sum.worst.lag} 个交易日）
              </span>
            )}
          </div>

          {/* 近 12 日覆盖度：一眼看出哪天只抓到一部分股票 */}
          {assets?.coverage?.days?.length ? (
            <CoverageStrip cov={assets.coverage} colors={colors} />
          ) : null}
        </div>
      )}

      {/* —— ① 数据资产清单 —— */}
      {assets && (
        <Card
          title="① 数据资产清单"
          colors={colors}
          extra={<span style={{ fontSize: 11.5, color: colors.muted }}>
            共 {sum?.n_total} 个数据集 · 统计于 {assets.generated_at?.slice(11, 19)}
          </span>}
        >
          {assets.groups.map((g) => {
            const open = openGroups[g.key] ?? false;
            return (
              <div key={g.key} style={{ marginBottom: 10, border: `1px solid ${colors.border}`, borderRadius: 9, overflow: "hidden" }}>
                <div
                  onClick={() => setOpenGroups({ ...openGroups, [g.key]: !open })}
                  style={{
                    display: "flex", alignItems: "center", gap: 9, padding: "8px 12px",
                    background: colors.tableStripe, cursor: "pointer",
                  }}
                >
                  <span style={{ fontSize: 11, color: colors.muted }}>{open ? "▼" : "▶"}</span>
                  <b style={{ fontSize: 13 }}>{g.label}</b>
                  <span style={{ fontSize: 11.5, color: colors.muted }}>{g.desc}</span>
                  <span style={{ flex: 1 }} />
                  <span className="num" style={{ fontSize: 11.5, color: colors.muted }}>{fmtRows(g.rows)} 行</span>
                  <Badge
                    text={g.bad ? `${g.bad} 项需注意` : "正常"}
                    color={g.bad ? colors.up : colors.down}
                    soft
                  />
                </div>
                {open && (
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                    <thead>
                      <tr style={{ color: colors.muted, fontSize: 11.5, textAlign: "left" }}>
                        <th style={{ padding: "6px 12px", fontWeight: 500 }}>数据集</th>
                        <th style={{ padding: "6px 8px", fontWeight: 500, textAlign: "right" }}>数据量</th>
                        <th style={{ padding: "6px 8px", fontWeight: 500, textAlign: "right" }}>覆盖标的</th>
                        <th style={{ padding: "6px 8px", fontWeight: 500 }}>时间范围</th>
                        <th style={{ padding: "6px 12px", fontWeight: 500 }}>新鲜度</th>
                      </tr>
                    </thead>
                    <tbody>
                      {g.items.map((it) => (
                        <AssetRow key={it.key} it={it} colors={colors} />
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            );
          })}
        </Card>
      )}

      {/* —— ② 数据源头 —— */}
      <Card
        title="② 数据源头"
        colors={colors}
        style={{ marginTop: 14 }}
        extra={<span style={{ fontSize: 11.5, color: colors.muted }}>配置文件 {lin.config_path}</span>}
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(280px,1fr))", gap: 10 }}>
          {lin.sources.map((s) => {
            const lr = s.last_run;
            const bound = assets?.by_source?.[s.name];
            return (
              <div
                key={s.name}
                style={{
                  background: colors.tableStripe, borderRadius: 9, padding: "10px 12px",
                  border: `1px solid ${s.enabled ? colors.border : colors.up}66`,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                  <span style={{ color: s.enabled ? colors.down : colors.up, fontSize: 13 }}>●</span>
                  <b style={{ fontSize: 13 }}>{s.name}</b>
                  <span style={{ flex: 1 }} />
                  <Badge text={s.enabled ? "启用" : "停用"} color={s.enabled ? colors.down : colors.up} soft />
                </div>
                <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 6 }}>{s.description}</div>

                {/* 存量：这个源现在一共攒了多少数据 */}
                <div style={{
                  fontSize: 11.5, padding: "5px 8px", borderRadius: 6, marginBottom: 5,
                  background: `${colors.card}`, border: `1px solid ${colors.border}`,
                }}>
                  {bound ? (
                    <>
                      <span style={{ color: colors.muted }}>存量 </span>
                      <b className="num" style={{ color: colors.text }}>{fmtRows(bound.rows)}</b>
                      <span style={{ color: colors.muted }}>{bound.key === "__bronze_text__" ? " 个文件" : " 行"}</span>
                      {bound.latest && (
                        <>
                          <span style={{ color: colors.muted }}> · 至 </span>
                          <span className="num" style={{ color: statusColor(bound.status, colors) }}>{fmtDate(bound.latest)}</span>
                        </>
                      )}
                      {bound.symbols ? (
                        <span style={{ color: colors.muted }}> · {bound.symbols.toLocaleString()} 只</span>
                      ) : null}
                    </>
                  ) : (
                    <span style={{ color: colors.muted }}>存量 —（未绑定数据集）</span>
                  )}
                </div>

                {/* 本次运行：rows=0 说清楚是「无新增」而不是「坏了」 */}
                <div style={{ fontSize: 11, display: "flex", gap: 8, alignItems: "center" }}>
                  <span style={{ color: statusColor(lr?.status || "", colors), fontWeight: 600 }}>
                    {lr ? (lr.status === "OK" ? "本次成功" : lr.status) : "未执行"}
                  </span>
                  <span className="num" style={{ color: colors.muted }}>
                    {lr ? (lr.rows > 0 ? `新增 ${lr.rows.toLocaleString()} 行` : "无新增（已最新）") : "—"}
                  </span>
                  {lr && <span className="num" style={{ color: colors.muted }}>{lr.duration_sec}s</span>}
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      {/* —— ③ 血缘流向图 —— */}
      <Card title="③ 数据血缘流向" colors={colors} style={{ marginTop: 14 }}>
        <LineageGraph lin={lin} assets={assets} colors={colors} />
      </Card>

      {/* —— ④ 处理步骤流水线 —— */}
      <Card title="④ 当前处理的内容（步骤流水线）" colors={colors} style={{ marginTop: 14 }} pad={0}>
        {lin.steps.map((s) => (
          <div
            key={s.name}
            style={{
              display: "flex", alignItems: "center", gap: 12, padding: "7px 16px",
              borderBottom: `1px solid ${colors.border}`,
            }}
          >
            <span className="num" style={{ width: 24, color: colors.muted, fontSize: 12 }}>{s.order}</span>
            <span style={{ width: 14, color: statusColor(s.status, colors), fontSize: 12 }}>
              {s.status === "OK" ? "●" : s.status === "FAIL" ? "✕" : "○"}
            </span>
            <span style={{ width: 190, fontSize: 12.5, fontFamily: "ui-monospace,Menlo,monospace" }}>{s.name}</span>
            <span style={{ flex: 1, fontSize: 11.5, color: colors.muted }}>
              {STEP_DESC[s.name] || s.name}
            </span>
            <span className="num" style={{ fontSize: 11.5, color: colors.muted, width: 78, textAlign: "right" }}>
              {s.rows > 0 ? `+${s.rows.toLocaleString()} 行` : "无新增"}
            </span>
            <span className="num" style={{ fontSize: 11.5, color: colors.muted, width: 42, textAlign: "right" }}>
              {s.duration_sec}s
            </span>
          </div>
        ))}
      </Card>

      {/* —— ⑤ 任务执行时间线 —— */}
      <Card title="⑤ 任务执行（最近 6 次运行）" colors={colors} style={{ marginTop: 14 }}>
        {lin.timeline.length === 0 && <div style={{ fontSize: 12, color: colors.muted }}>暂无运行记录</div>}
        {lin.timeline.map((r) => {
          const maxSec = Math.max(...r.steps.map((s) => s.duration_sec), 0.1);
          return (
            <div key={r.run_id} style={{ marginBottom: 12 }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 5, flexWrap: "wrap" }}>
                <Badge text={r.status} color={statusColor(r.status, colors)} soft />
                <span className="num" style={{ fontSize: 12.5, fontWeight: 600 }}>#{r.run_id}</span>
                <span style={{ fontSize: 11.5, color: colors.muted }}>
                  {r.trigger} · {r.started_at?.slice(5, 16).replace("T", " ")} · 耗时 {r.total_sec}s
                </span>
                {r.error && <span style={{ fontSize: 11, color: colors.up }}>{r.error.slice(0, 90)}</span>}
              </div>
              <div style={{ display: "flex", gap: 3, alignItems: "flex-end", height: 26 }}>
                {r.steps.map((s) => (
                  <div
                    key={s.name}
                    title={`${s.name} · ${s.duration_sec}s · ${s.rows}行`}
                    style={{
                      flex: 1, minWidth: 22, maxWidth: 90,
                      height: Math.max(5, (s.duration_sec / maxSec) * 26),
                      background: statusColor(s.status, colors),
                      opacity: s.status === "OK" ? 0.5 : 0.95, borderRadius: 3,
                    }}
                  />
                ))}
              </div>
            </div>
          );
        })}
      </Card>

      {/* —— ⑥ 数据健康度 —— */}
      {health && (
        <Card title="⑥ 数据健康度（规则化检查）" colors={colors} style={{ marginTop: 14 }}>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
            <ScoreRing score={health.overall_score} status={health.overall_status} colors={colors} />
            <div style={{ flex: 1, minWidth: 260 }}>
              {Object.entries(health.layers).map(([k, ly]) => (
                <div key={k} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 7 }}>
                  <span style={{ width: 62, fontSize: 12, color: colors.muted }}>{ly.label}</span>
                  <div style={{ flex: 1, height: 8, background: colors.tableStripe, borderRadius: 4, overflow: "hidden" }}>
                    <div style={{
                      width: `${Math.min(100, ly.score)}%`, height: "100%",
                      background: ly.status === "healthy" ? colors.down : ly.status === "warn" ? colors.accent : colors.up,
                    }} />
                  </div>
                  <span className="num" style={{ width: 34, fontSize: 12, textAlign: "right" }}>{ly.score}</span>
                  <span style={{ fontSize: 11, color: colors.muted, width: 52 }}>
                    {ly.checks.filter((c) => c.status === "ok").length}/{ly.checks.length} 通过
                  </span>
                </div>
              ))}
            </div>
          </div>
          {health.alerts.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {health.alerts.map((a, i) => (
                <span
                  key={i}
                  style={{
                    fontSize: 11.5, padding: "3px 10px", borderRadius: 6,
                    background: a.level === "error" ? `${colors.up}12` : `${colors.accent}12`,
                    border: `1px solid ${a.level === "error" ? colors.up : colors.accent}`,
                  }}
                >
                  {a.layer} · {a.check}：{a.detail}
                </span>
              ))}
            </div>
          )}
        </Card>
      )}

      {/* —— ⑦ 健康度规则管理 —— */}
      <Card
        title="⑦ 健康度检查规则（可配置）"
        colors={colors}
        style={{ marginTop: 14 }}
        extra={
          <button
            onClick={() => setRulesOpen(!rulesOpen)}
            style={{ fontSize: 12, color: colors.accent, background: "none", border: 0, cursor: "pointer" }}
          >
            {rulesOpen ? "收起" : "管理规则"}
          </button>
        }
      >
        {!rulesOpen ? (
          <div style={{ fontSize: 12.5, color: colors.muted }}>
            {rules.filter((r) => r.enabled).length} 条规则生效中 · 点击右上角管理（启停/改阈值/增删）
          </div>
        ) : (
          <div>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ color: colors.muted, textAlign: "left" }}>
                  <th style={{ padding: "6px 8px", fontWeight: 500 }}>名称</th>
                  <th style={{ fontWeight: 500 }}>层</th>
                  <th style={{ fontWeight: 500 }}>指标</th>
                  <th style={{ fontWeight: 500 }}>条件</th>
                  <th style={{ fontWeight: 500 }}>上次值</th>
                  <th style={{ fontWeight: 500 }}>状态</th>
                  <th style={{ fontWeight: 500 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {rules.map((r) => (
                  <tr key={r.id} style={{ borderTop: `1px solid ${colors.border}` }}>
                    <td style={{ padding: "6px 8px" }} title={r.metric_doc}>{r.name}</td>
                    <td style={{ fontSize: 11.5, color: colors.muted }}>{r.layer}</td>
                    <td style={{ fontSize: 11.5 }}><code>{r.metric}</code></td>
                    <td className="num" style={{ fontSize: 12 }}>
                      {editing === r.id ? (
                        <input
                          value={editTh}
                          onChange={(e) => setEditTh(e.target.value)}
                          style={{
                            width: 70, padding: "2px 6px", border: `1px solid ${colors.border}`,
                            background: colors.card, color: colors.text,
                          }}
                        />
                      ) : (
                        <>{r.comparator} {r.threshold ?? "—"}</>
                      )}
                    </td>
                    <td className="num" style={{ fontSize: 11.5, color: colors.muted }}>{r.last_value ?? "—"}</td>
                    <td>
                      <span style={{ color: r.last_status === "ok" ? colors.down : colors.up }}>
                        {r.last_status === "ok" ? "✓" : r.last_status === "error" ? "✕" : "◐"}
                      </span>{" "}
                      {!r.enabled && <span style={{ fontSize: 11, color: colors.muted }}>(停用)</span>}
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {editing === r.id ? (
                        <>
                          <button
                            onClick={async () => {
                              await api.healthRuleSave(r.id, { threshold: parseFloat(editTh) || 0 });
                              setEditing(null);
                              refresh();
                            }}
                            style={{ fontSize: 11.5, color: colors.down, background: "none", border: 0, cursor: "pointer" }}
                          >保存</button>
                          <button
                            onClick={() => setEditing(null)}
                            style={{ fontSize: 11.5, color: colors.muted, background: "none", border: 0, cursor: "pointer" }}
                          >取消</button>
                        </>
                      ) : (
                        <>
                          <button
                            onClick={() => { setEditing(r.id); setEditTh(String(r.threshold ?? 0)); }}
                            style={{ fontSize: 11.5, color: colors.accent, background: "none", border: 0, cursor: "pointer" }}
                          >改阈值</button>
                          <button
                            onClick={async () => { await api.healthRuleToggle(r.id, !r.enabled); refresh(); }}
                            style={{ fontSize: 11.5, color: colors.muted, background: "none", border: 0, cursor: "pointer" }}
                          >{r.enabled ? "停用" : "启用"}</button>
                          <button
                            onClick={async () => {
                              if (window.confirm(`删除规则「${r.name}」？`)) {
                                await api.healthRuleDelete(r.id);
                                refresh();
                              }
                            }}
                            style={{ fontSize: 11.5, color: colors.up, background: "none", border: 0, cursor: "pointer" }}
                          >删除</button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div style={{ marginTop: 8, fontSize: 11.5, color: colors.muted }}>
              新增规则示例：POST /api/v1/monitor/health/rules {"{ name, layer, metric: freshness, params: {table: 'kline_daily'}, comparator: '<=', threshold: 5 }"}
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

/* ============ 近 12 日覆盖度条 ============ */
function CoverageStrip({ cov, colors }: { cov: CoverageBlock; colors: ThemeColors }) {
  const max = Math.max(cov.median, ...cov.days.map((d) => d.symbols), 1);
  return (
    <div style={{ marginTop: 10, paddingTop: 10, borderTop: `1px dashed ${colors.border}` }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 5 }}>
        <span style={{ fontSize: 11.5, color: colors.muted }}>
          近 {cov.days.length} 个交易日，个股日K 每天入库的标的数量
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 11, color: colors.muted }}>峰值 {cov.peak ?? cov.median}</span>
        {cov.pending_date && (
          <span style={{ fontSize: 11.5, color: colors.muted }}>
            {cov.pending_date.slice(5)} 盘中，16:00 后发布
          </span>
        )}
        {cov.partial_count > 0 && (
          <span style={{ fontSize: 11.5, color: colors.up, fontWeight: 600 }}>
            {cov.partial_count} 天残缺（抓取不完整）
          </span>
        )}
      </div>
      <div style={{ display: "flex", gap: 3, alignItems: "flex-end", height: 46 }}>
        {cov.days.map((d) => {
          const h = Math.max(3, (d.symbols / max) * 34);
          // 待发布的当天用灰色虚框：既显示「已有少量数据」，又不误报为断供
          const tone = d.pending ? colors.muted : d.partial ? colors.up : colors.down;
          return (
            <div key={d.date} style={{ flex: 1, minWidth: 18, textAlign: "center" }}
              title={d.pending
                ? `${d.date}：当日数据尚未发布（16:00 后可拉）`
                : `${d.date}：${d.symbols} 只${d.partial ? "（残缺）" : ""}`}>
              <div style={{ fontSize: 9.5, color: colors.muted, marginBottom: 2 }} className="num">
                {d.symbols}
              </div>
              <div style={{
                height: h, background: tone, opacity: d.partial ? 0.95 : d.pending ? 0.2 : 0.55,
                borderRadius: 3,
                border: d.partial ? `1px solid ${colors.up}`
                  : d.pending ? `1px dashed ${colors.muted}` : "none",
              }} />
              <div style={{
                fontSize: 9, marginTop: 2,
                color: d.partial ? colors.up : d.pending ? colors.muted : colors.muted,
              }}>
                {d.date.slice(5).replace("-", "/")}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ============ 资产清单单行 ============ */
function AssetRow({ it, colors }: { it: AssetItem; colors: ThemeColors }) {
  const tone = statusColor(it.status, colors);
  const meta = STATUS_META[it.status] || STATUS_META.warn;
  // 新鲜度条：以 max_lag*3 为满格基准
  const cap = Math.max(1, it.max_lag * 3 || 1);
  const ratio = it.lag_trading_days === null ? 0 : Math.min(1, it.lag_trading_days / cap);
  return (
    <tr style={{ borderTop: `1px solid ${colors.border}` }}>
      <td style={{ padding: "7px 12px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 7, height: 7, borderRadius: 4, background: tone, display: "inline-block" }} />
          <b style={{ fontSize: 12.5 }}>{it.label}</b>
        </div>
        <div style={{ fontSize: 10.5, color: colors.muted, marginTop: 2, paddingLeft: 13 }}>{it.note}</div>
      </td>
      <td className="num" style={{ padding: "7px 8px", textAlign: "right", fontSize: 12.5 }}>
        {fmtRows(it.rows)}
        <div style={{ fontSize: 10, color: colors.muted }}>
          {it.key === "__bronze_text__" || it.key === "__silver__" ? "文件" : "行"}
        </div>
      </td>
      <td className="num" style={{ padding: "7px 8px", textAlign: "right", fontSize: 12.5 }}>
        {it.symbols != null ? it.symbols.toLocaleString() : "—"}
        <div style={{ fontSize: 10, color: colors.muted }}>
          {it.symbols != null ? (it.key === "factor_registry" ? "登记" : "标的") : ""}
        </div>
      </td>
      <td style={{ padding: "7px 8px", fontSize: 11.5, color: colors.muted, whiteSpace: "nowrap" }}>
        {it.start ? `${fmtDate(it.start)} →` : "—"}
        <div className="num" style={{ fontSize: 12, color: tone }}>{fmtDate(it.latest)}</div>
      </td>
      <td style={{ padding: "7px 12px", minWidth: 168 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
          <div style={{ flex: 1, height: 6, background: colors.tableStripe, borderRadius: 3, overflow: "hidden" }}>
            <div style={{ width: `${ratio * 100}%`, height: "100%", background: tone }} />
          </div>
          <span style={{ fontSize: 11.5, color: tone, whiteSpace: "nowrap" }}>
            {it.status === "empty" ? meta.short : it.lag_trading_days === null ? meta.short : `滞后 ${it.lag_trading_days} 日`}
          </span>
        </div>
        <div style={{ fontSize: 10, color: colors.muted, marginTop: 2 }} title={meta.text}>
          {it.lag_calendar_days !== null ? `自然日 ${it.lag_calendar_days} 天 · ` : ""}
          {meta.text}
        </div>
      </td>
    </tr>
  );
}

/* ============ 健康分环 ============ */
function ScoreRing({ score, status, colors }: { score: number; status: string; colors: ThemeColors }) {
  const tone = status === "healthy" ? colors.down : status === "warn" ? colors.accent : colors.up;
  const R = 34, C = 2 * Math.PI * R;
  return (
    <div style={{ position: "relative", width: 88, height: 88, flexShrink: 0 }}>
      <svg width={88} height={88} style={{ transform: "rotate(-90deg)" }}>
        <circle cx={44} cy={44} r={R} fill="none" stroke={colors.tableStripe} strokeWidth={8} />
        <circle
          cx={44} cy={44} r={R} fill="none" stroke={tone} strokeWidth={8} strokeLinecap="round"
          strokeDasharray={`${(C * Math.min(100, score)) / 100} ${C}`}
        />
      </svg>
      <div style={{
        position: "absolute", inset: 0, display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center",
      }}>
        <b className="num" style={{ fontSize: 21, color: tone }}>{score}</b>
        <span style={{ fontSize: 10, color: colors.muted }}>健康分</span>
      </div>
    </div>
  );
}

/* ============ 血缘图（SVG，高度随数据源数量自适应） ============ */
function LineageGraph({ lin, assets, colors }: { lin: LineageReport; assets: AssetsReport | null; colors: ThemeColors }) {
  const c = colors;
  const colW = 196, gap = 42;
  const rowH = 54, topPad = 34;
  // 关键修复：画布高度随数据源数量增长，否则源多时被裁掉
  const H = Math.max(250, topPad + lin.sources.length * rowH + 24);
  const midY = H / 2;
  const x1 = 12;
  const x2 = x1 + colW + gap;
  const x3 = x2 + colW + gap;
  const x4 = x3 + colW + gap;
  const x5 = x4 + colW + gap;
  const W = x5 + 76;

  const box = (x: number, y: number, w: number, h: number, title: string, sub: string, tone: string, key: string) => (
    <g key={key}>
      <rect x={x} y={y} width={w} height={h} rx={8} fill={c.card} stroke={tone} strokeWidth={1.2} />
      <text x={x + 10} y={y + 19} fill={c.text} fontSize={11.5} fontWeight={600}>{title}</text>
      <text x={x + 10} y={y + 35} fill={c.muted} fontSize={10}>{sub}</text>
    </g>
  );

  /** 曲线连接：从 (x1,y1) 到 (x2,y2) */
  const link = (sx: number, sy: number, tx: number, ty: number, key: string) => {
    const mx = (sx + tx) / 2;
    return (
      <path
        key={key} d={`M${sx} ${sy} C${mx} ${sy} ${mx} ${ty} ${tx - 6} ${ty}`}
        stroke={c.muted} strokeWidth={1.2} fill="none" opacity={0.55}
      />
    );
  };

  const bronzeTotal = Object.values(lin.layers.bronze).reduce((n, v) => n + v.files, 0);
  const bronzeMb = Object.values(lin.layers.bronze).reduce((n, v) => n + v.size_mb, 0);
  const silverN = Object.keys(lin.layers.silver.files).length;
  const g = lin.layers.gold;
  const stockRows = assets?.by_source?.["stock_kline_core"]?.rows;

  const boxH = 52;
  const bY = midY - boxH / 2;

  return (
    <div style={{ overflowX: "auto" }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ minWidth: 860 }}>
        {["数据源", "Bronze 原始层", "Silver 清洗/打分", "Gold 因子与情绪", "应用"].map((t, i) => (
          <text key={t} x={[x1, x2, x3, x4, x5][i]} y={14} fill={c.muted} fontSize={10.5} fontWeight={600}>{t}</text>
        ))}

        {/* 数据源 → Bronze */}
        {lin.sources.map((s, i) => {
          const y = topPad + i * rowH;
          const cy = y + 22;
          const bound = assets?.by_source?.[s.name];
          return (
            <g key={s.name}>
              {box(x1, y, colW, 44, s.name,
                `${s.enabled ? "启用" : "停用"} · ${bound ? `${fmtRows(bound.rows)}${bound.key === "__bronze_text__" ? "文件" : "行"}` : "—"} · ${fmtDate(bound?.latest)}`,
                s.enabled ? (bound && bound.status === "ok" ? c.down : c.accent) : c.up, `s${i}`)}
              {link(x1 + colW, cy, x2, midY, `l${i}`)}
            </g>
          );
        })}

        {/* Bronze */}
        {box(x2, bY, colW, boxH, "原始快照 Parquet",
          `${bronzeTotal} 文件 · ${bronzeMb.toFixed(1)}MB`, c.accent, "b")}
        {link(x2 + colW, midY, x3, midY, "l-b")}

        {/* Silver */}
        {box(x3, bY, colW, boxH, "清洗 + 情绪打分",
          `${silverN} 文件 · 质检${lin.layers.silver.quality ? "已出" : "无"}`, c.accent, "s")}
        {link(x3 + colW, midY, x4, midY, "l-s")}

        {/* Gold */}
        <g key="gold">
          <rect x={x4} y={bY - 24} width={colW} height={boxH + 48} rx={8} fill={c.card} stroke={c.accent} strokeWidth={1.2} />
          <text x={x4 + 10} y={bY - 6} fill={c.text} fontSize={11.5} fontWeight={600}>因子与情绪表</text>
          <text x={x4 + 10} y={bY + 11} fill={c.muted} fontSize={10}>
            个股K线 {(g.tables.kline_daily ?? 0).toLocaleString()} 行
          </text>
          <text x={x4 + 10} y={bY + 27} fill={c.muted} fontSize={10}>
            基础因子 {(g.tables.factor_daily ?? 0).toLocaleString()} · 挖掘 {(g.tables.factor_mined_daily ?? 0).toLocaleString()}
          </text>
          <text x={x4 + 10} y={bY + 43} fill={c.muted} fontSize={10}>
            市场情绪 {g.tables.news_market_daily ?? 0} 天 · 个股情绪 {(g.tables.news_stock_daily ?? 0).toLocaleString()} 行
          </text>
          <text x={x4 + 10} y={bY + 59} fill={c.up} fontSize={10}>
            K线至 {fmtDate(g.latest.kline)} / 情绪至 {fmtDate(g.latest.news)}
          </text>
        </g>
        {link(x4 + colW, midY, x5, midY, "l-g")}

        {/* 应用 */}
        {box(x5, bY - 6, 78, boxH + 12, "回测 / 选股",
          stockRows ? `${fmtRows(stockRows)} 行可用` : "组合·寻优", c.muted, "app")}
      </svg>
    </div>
  );
}
