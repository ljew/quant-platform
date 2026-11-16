/**
 * 到达时点（数据中心 · Tab）：每类数据「源端几点可得 → 计划几点处理 → 实际几点落库」。
 *
 * 与两个邻近页面的分工：
 *   · 数据健康 —— 这张表现在新鲜吗（表维度、落后几天）
 *   · 数据管道 —— 这次 run 每一步跑了多久（单次执行）
 *   · 本页     —— 每天几点该到、实际几点到的、卡在哪一段（时钟维度）
 *
 * 为什么要这一页（2026-11-16 两个真实问题，前两页都看不出来）：
 *   ① 基金 nav 原来排 19:40，而净值当晚 20:00~23:00 才陆续公布 —— 排期早于发布，
 *      每天必然差一天；在「新鲜度」里只是「落后 1 天」，看着像数据源的问题。
 *      （2026-11-16 已后移到 21:00，并给 nav 加了「净值是否追平」的后置断言）
 *   ② 11-13 关机、11-16 开机，基金管道一条 run 都没有 —— 页面上也只是「落后 N 天」，
 *      分不清「压根没跑」和「跑了没抓到」。本页左侧时刻线是空的，一目了然。
 *
 * 三个时间口径写在表头上（带 title 提示），别让读者自己猜：
 *   源端可用 = 经验值，不是承诺；计划处理 = 调度常量；实际落库 = 处理完成、库里可查。
 */
import { Fragment, useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import { api, type DataTimeline, type TimelineDomain } from "../api/client";
import { useTheme } from "../theme";

const th: CSSProperties = {
  textAlign: "left", padding: "8px 12px", fontWeight: 500, whiteSpace: "nowrap",
};

const GROUP_ORDER = ["行情", "资讯", "派生", "ETF", "基金"];
const GROUP_COLOR: Record<string, string> = {
  行情: "#2456c8",
  资讯: "#b45309",
  派生: "#7c3aed",
  ETF: "#0891b2",
  基金: "#be123c",
  调度: "#64748b",
};

function statusColor(s: string, colors: ReturnType<typeof useTheme>["colors"]) {
  if (s === "FAIL") return colors.up;
  if (s === "WARN") return "#d97706";
  if (s === "OK") return colors.down;
  return colors.muted;
}

/** 「ETL·extract_index_kline」→ 「ETL」：列宽只有一百来像素，全名会把行挤爆。 */
function srcTag(by: string | null | undefined): string {
  return (by ?? "").split("·")[0];
}

/** 时刻只显示 HH:MM:SS；跨天补 MM-DD。 */
function hms(v: string | null | undefined): string {
  if (!v) return "—";
  const s = String(v).replace(" ", "T");
  return s.length >= 19 ? s.slice(11, 19) : String(v);
}
function todayStr(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function when(v: string | null | undefined): string {
  if (!v) return "—";
  const s = String(v).replace(" ", "T");
  if (s.slice(0, 10) === todayStr()) return hms(s);
  return `${s.slice(5, 10)} ${hms(s)}`;
}

export default function DataTimelinePage() {
  const { colors } = useTheme();
  const [data, setData] = useState<DataTimeline | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [onlyBad, setOnlyBad] = useState(false);
  const [group, setGroup] = useState<string>("全部");

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      setData(await api.dataTimeline(1));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    // 30 秒自动刷新：这一页看的就是「现在到哪一步了」，手动刷会错过正在进行的那一段
    const t = window.setInterval(load, 30_000);
    return () => window.clearInterval(t);
  }, [load]);

  const domains = useMemo(() => {
    let rows = data?.domains ?? [];
    if (group !== "全部") rows = rows.filter((r) => r.group === group);
    if (onlyBad) rows = rows.filter((r) => r.status === "FAIL" || r.status === "WARN");
    return rows;
  }, [data, group, onlyBad]);

  const counts = data?.counts ?? {};
  const events = data?.events ?? [];

  // 按组小计，给「全部」视图做视觉分段
  const grouped = useMemo(() => {
    const out: { g: string; rows: TimelineDomain[] }[] = [];
    for (const r of domains) {
      const last = out[out.length - 1];
      if (last && last.g === r.group) last.rows.push(r);
      else out.push({ g: r.group, rows: [r] });
    }
    return out;
  }, [domains]);

  const failRows = (data?.domains ?? []).filter((r) => r.status === "FAIL");

  return (
    <div>
      {/* ── 头部 ── */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 6 }}>
        <h2 style={{ margin: 0, fontSize: 19 }}>到达时点</h2>
        <span style={{ color: colors.muted, fontSize: 12 }}>
          {data
            ? `${data.domains.length} 个数据域 · 期望行情日 ${data.expected_market_date} · ${data.is_trading_day ? "交易日" : "非交易日"} · ${data.now.replace("T", " ").slice(0, 19)}`
            : "加载中…"}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
          <select
            value={group}
            onChange={(e) => setGroup(e.target.value)}
            style={{
              padding: "5px 8px", fontSize: 12, borderRadius: 6,
              border: `1px solid ${colors.border}`, background: colors.card, color: colors.text,
            }}
          >
            {["全部", ...GROUP_ORDER].map((g) => (
              <option key={g} value={g}>{g}</option>
            ))}
          </select>
          <button
            onClick={() => setOnlyBad((v) => !v)}
            style={{
              padding: "5px 12px", fontSize: 12, cursor: "pointer", borderRadius: 6,
              border: `1px solid ${colors.border}`,
              background: onlyBad ? colors.accent : colors.card,
              color: onlyBad ? "#fff" : colors.text,
            }}
          >
            {onlyBad ? "只看异常" : "全部"}
          </button>
          <button
            onClick={load}
            disabled={loading}
            style={{
              padding: "5px 12px", fontSize: 12, cursor: "pointer", borderRadius: 6,
              border: `1px solid ${colors.border}`, background: colors.card, color: colors.text,
            }}
          >
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>

      {err && (
        <div style={{
          background: colors.card, border: `1px solid ${colors.up}`, color: colors.up,
          borderRadius: 8, padding: "10px 12px", fontSize: 12.5, margin: "10px 0",
        }}>
          {err}
        </div>
      )}

      {/* ── KPI ── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, margin: "14px 0 16px" }}>
        {[
          { k: "FAIL", label: "断档", hint: "落后 ≥2 个交易日，或最近一次失败" },
          { k: "WARN", label: "滞后", hint: "落后 1 个交易日" },
          { k: "OK", label: "就绪", hint: "已是最新交易日" },
          { k: "n/a", label: "不判", hint: "披露期/实时类，不按日判" },
        ].map((it) => (
          <div key={it.k} style={{
            background: colors.card, border: `1px solid ${colors.border}`,
            borderRadius: 10, padding: "12px 14px",
          }}>
            <div style={{ fontSize: 11, color: colors.muted }}>{it.label}</div>
            <div style={{ fontSize: 24, fontWeight: 700, lineHeight: 1.25, color: statusColor(it.k, colors) }}>
              {counts[it.k] ?? 0}
            </div>
            <div style={{ fontSize: 10.5, color: colors.muted }}>{it.hint}</div>
          </div>
        ))}
      </div>

      {/* ── 结论条：只在对不齐时出现 ── */}
      {failRows.length > 0 && (
        <div style={{
          background: colors.card, border: `1px solid ${colors.up}`, borderRadius: 10,
          padding: "10px 14px", marginBottom: 16, fontSize: 12.5, lineHeight: 1.9,
        }}>
          <div style={{ color: colors.up, fontWeight: 600, marginBottom: 4 }}>
            需要处理（{failRows.length}）
          </div>
          {failRows.map((r) => (
            <div key={r.key} style={{ marginLeft: 2 }}>
              <b>{r.name}</b>
              <span style={{ color: colors.muted }}>
                {"　"}最新 {r.data_date ?? "空"}
                {r.lag_days ? ` · 落后 ${r.lag_days} 个交易日` : ""}
                {r.note ? ` · ${r.note}` : ""}
                {"　"}<span style={{ opacity: 0.7 }}>· {r.source}</span>
              </span>
            </div>
          ))}
        </div>
      )}

      {/* ── 数据域时刻表 ── */}
      <div style={{
        background: colors.card, border: `1px solid ${colors.border}`,
        borderRadius: 10, overflow: "hidden", marginBottom: 18,
      }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
          <thead>
            <tr style={{ background: colors.tableStripe, color: colors.muted }}>
              <th style={th}>状态</th>
              <th style={th}>数据域</th>
              <th style={th} title="这批数据的来源接口">源</th>
              <th style={{ ...th, textAlign: "right" }} title="源端经验可获取时点（不是承诺，写错会在下一列暴露）">源端可用</th>
              <th style={{ ...th, textAlign: "right" }} title="平台调度的触发时点（从各 scheduler 常量读）">计划处理</th>
              <th style={{ ...th, textAlign: "right" }} title="库里最新数据是哪一天">最新数据日</th>
              <th style={{ ...th, textAlign: "right" }} title="这批数据处理完成、库里可查的时刻">实际落库</th>
              <th style={{ ...th, textAlign: "right" }} title="最新数据日比期望交易日少几天">落后</th>
            </tr>
          </thead>
          <tbody>
            {grouped.map((blk) => (
              <Fragment key={`blk-${blk.g}`}>
                <tr style={{ background: colors.tableStripe }}>
                  <td colSpan={8} style={{
                    padding: "6px 12px", fontSize: 11, fontWeight: 600,
                    color: GROUP_COLOR[blk.g] ?? colors.muted, letterSpacing: 0.5,
                  }}>
                    {blk.g} · {blk.rows.length}
                  </td>
                </tr>
                {blk.rows.map((r) => (
                  <tr key={r.key} style={{ borderTop: `1px solid ${colors.border}` }}>
                    <td style={{ padding: "7px 12px", fontWeight: 600, color: statusColor(r.status, colors) }}>
                      {r.status}
                    </td>
                    <td style={{ padding: "7px 12px" }}>
                      {r.name}
                      <span style={{ color: colors.muted, marginLeft: 8, fontSize: 10.5 }}>{r.owner}</span>
                      {r.note ? (
                        <span style={{ color: "#b45309", marginLeft: 8, fontSize: 10.5 }}>· {r.note}</span>
                      ) : null}
                    </td>
                    <td style={{ padding: "7px 12px", color: colors.muted, fontSize: 11.5 }}>{r.source}</td>
                    <td style={{ padding: "7px 12px", textAlign: "right", color: colors.muted }}>{r.ready}</td>
                    <td style={{ padding: "7px 12px", textAlign: "right", color: colors.muted }}>{r.plan}</td>
                    <td style={{ padding: "7px 12px", textAlign: "right", fontFamily: "ui-monospace,Menlo,monospace" }}>
                      {r.data_date ?? "—"}
                    </td>
                    <td style={{ padding: "7px 12px", textAlign: "right", fontFamily: "ui-monospace,Menlo,monospace", color: colors.muted, whiteSpace: "nowrap" }}>
                      {when(r.landed_at)}
                      {/* 第二行只留「来源」（ETL/基金/调度/表/文件），全名太长会把列撑爆；
                          完整来源与落库偏差放 title，悬停可查。
                          偏差只报 30~180 分这一档：超过 3 小时的不是「晚点」，而是当天
                          另有一次手动跑（11-16 14:46 补公众号），显示「晚 506 分」纯属误导。 */}
                      <div
                        style={{ fontSize: 10, opacity: 0.7 }}
                        title={`${r.landed_by ?? ""}${r.late_min != null ? ` · 比计划${r.late_min > 0 ? "晚" : "早"} ${Math.abs(r.late_min)} 分` : ""}`}
                      >
                        {srcTag(r.landed_by)}
                        {r.late_min != null && r.late_min > 30 && r.late_min <= 180 && (
                          <span style={{ color: "#d97706" }}> 晚 {r.late_min} 分</span>
                        )}
                      </div>
                    </td>
                    <td style={{
                      padding: "7px 12px", textAlign: "right", whiteSpace: "nowrap",
                      // 颜色跟状态走：status=OK 时即使 lag=1（还没到计划时点）也不该标黄，
                      // 否则「状态 OK / 落后 1 天 橙色」自相矛盾。
                      color: r.lag_days && r.status !== "OK"
                        ? statusColor(r.lag_days >= 2 ? "FAIL" : "WARN", colors)
                        : colors.muted,
                    }}>
                      {r.lag_days == null ? "—" : r.lag_days === 0 ? "最新" : `${r.lag_days} 天`}
                    </td>
                  </tr>
                ))}
              </Fragment>
            ))}
            {domains.length === 0 && (
              <tr>
                <td colSpan={8} style={{ padding: "20px 12px", textAlign: "center", color: colors.muted }}>
                  {onlyBad ? "没有异常 —— 换「全部」看完整清单" : "没有数据"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* ── 下半场：今日时刻线 + 每日计划 ── */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.55fr) minmax(0, 1fr)", gap: 16 }}>
        <div style={{
          background: colors.card, border: `1px solid ${colors.border}`,
          borderRadius: 10, padding: "12px 14px",
        }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 8 }}>
            <b style={{ fontSize: 13.5 }}>今日时刻线</b>
            <span style={{ fontSize: 11, color: colors.muted }}>
              {events.length} 个节点 · 按发生顺序（ETL 每步、编排每步、基金每阶段、各调度器触发）
            </span>
          </div>
          {events.length === 0 && (
            <div style={{ color: colors.muted, fontSize: 12.5, padding: "14px 0", lineHeight: 1.9 }}>
              今天还没有任何处理记录。<br />
              如果今天是交易日且已过 19:00，这本身就是故障信号 ——
              说明 ETL / 编排一条都没跑（去「数据管道」看最近一次 run 的日期）。
            </div>
          )}
          {/* 不设死高度会跟右侧两个卡片一起撑长，设 900 差不多能一次看到收工；
              再长（一天 100+ 节点，比如编排把每个策略都记一条）就滚动。 */}
          <div style={{ maxHeight: 900, overflowY: "auto", paddingRight: 4 }}>
            {events.map((e, i) => (
              <div
                key={`${e.at}-${i}`}
                style={{
                  display: "grid", gridTemplateColumns: "62px 8px minmax(0,1fr)",
                  gap: 10, padding: "5px 0",
                  borderTop: i ? `1px solid ${colors.border}` : undefined,
                }}
              >
                <div style={{
                  fontFamily: "ui-monospace,Menlo,monospace", fontSize: 11.5,
                  color: colors.muted, paddingTop: 1,
                }}>
                  {hms(e.at)}
                </div>
                <div style={{
                  width: 6, height: 6, borderRadius: 3, marginTop: 6,
                  background: e.status === "FAIL" ? colors.up
                    : e.status === "WARN" ? "#d97706"
                      : GROUP_COLOR[e.group] ?? colors.muted,
                }} />
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 12.5 }}>
                    <span style={{
                      fontSize: 10, color: GROUP_COLOR[e.group] ?? colors.muted,
                      marginRight: 6, fontWeight: 600,
                    }}>
                      {e.group}
                    </span>
                    {e.label}
                    {e.status && e.status !== "ok" && e.status !== "OK" && (
                      <span style={{ color: statusColor(e.status, colors), marginLeft: 6, fontSize: 11 }}>
                        {e.status}
                      </span>
                    )}
                  </div>
                  {e.detail ? (
                    <div style={{
                      fontSize: 10.5, color: colors.muted, lineHeight: 1.6,
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }} title={e.detail}>
                      {e.detail}
                    </div>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
          <div style={{
            background: colors.card, border: `1px solid ${colors.border}`,
            borderRadius: 10, padding: "12px 14px",
          }}>
            <div style={{ fontSize: 13.5, fontWeight: 700, marginBottom: 8 }}>每日计划</div>
            {(data?.schedule ?? []).map((s) => (
              <div
                key={`${s.at}-${s.label}`}
                style={{
                  display: "grid", gridTemplateColumns: "82px minmax(0,1fr)",
                  gap: 8, padding: "4px 0", fontSize: 12,
                }}
              >
                <div style={{ fontFamily: "ui-monospace,Menlo,monospace", color: colors.muted, fontSize: 11.5 }}>
                  {s.at}
                </div>
                <div>
                  <span style={{ color: GROUP_COLOR[s.group] ?? colors.muted, marginRight: 6, fontSize: 10 }}>
                    {s.group}
                  </span>
                  {s.label}
                  <div style={{ fontSize: 10.5, color: colors.muted }}>{s.owner}</div>
                </div>
              </div>
            ))}
          </div>

          <div style={{
            background: colors.card, border: `1px solid ${colors.border}`,
            borderRadius: 10, padding: "12px 14px",
          }}>
            <div style={{ fontSize: 13.5, fontWeight: 700, marginBottom: 6 }}>最近一次 ETL</div>
            {(data?.etl_runs ?? []).slice(0, 1).map((r) => (
              <div key={r.run_id} style={{ fontSize: 12, lineHeight: 1.9 }}>
                <div style={{ color: colors.muted }}>
                  run #{r.run_id} · {r.trigger} ·{" "}
                  <b style={{ color: statusColor(r.status, colors) }}>{r.status}</b>
                </div>
                <div style={{ color: colors.muted, fontSize: 11.5 }}>
                  {when(r.started_at)} → {hms(r.finished_at)}
                  {r.started_at && r.finished_at ? ` · ${Math.max(0, Math.round(
                    (Date.parse(`${r.finished_at}`) - Date.parse(`${r.started_at}`)) / 1000))}s` : ""}
                </div>
                <div style={{ marginTop: 6 }}>
                  {r.steps.slice(0, 18).map((s) => (
                    <div key={s.name} style={{
                      display: "grid", gridTemplateColumns: "minmax(0,1fr) 52px 74px",
                      gap: 6, fontSize: 11.5, padding: "1px 0", color: colors.muted,
                    }}>
                      <span style={{
                        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                        color: s.status === "FAIL" ? colors.up : s.status === "WARN" ? "#d97706" : colors.text,
                      }}>
                        {s.name}
                      </span>
                      <span style={{ textAlign: "right" }}>{s.duration_sec}s</span>
                      <span style={{ textAlign: "right" }}>{s.rows.toLocaleString("zh-CN")}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {!(data?.etl_runs ?? []).length && (
              <div style={{ fontSize: 12, color: colors.muted }}>没有运行记录</div>
            )}
          </div>
        </div>
      </div>

      <div style={{ marginTop: 14, fontSize: 11.5, color: colors.muted, lineHeight: 1.9 }}>
        <b style={{ color: colors.text }}>怎么读</b>：<code>源端可用</code>是经验值（源什么时候能拿到），
        <code>计划处理</code>是调度常量，<code>实际落库</code>是处理完、库里能查到的时刻。
        三者对比才能看出问题：实际落库早于源端可用 = <b>排期排在了数据发布之前</b>，
        每天必然差一天（基金净值就是这类）；实际落库是空的 = <b>压根没跑</b>（看左下时刻线有没有节点）。
        「落后」按<b>交易日</b>算，周末和节假日不会误报。
      </div>
    </div>
  );
}

