/**
 * 策略体检卡（原「稳健摘要」）：挂在首页**最下方**。
 *
 * 2026-11-08 老刘问「稳健摘要这一部分具体干啥用的，如果需要的放在最下方」——
 * 它回答的是「我的策略框架健康吗」（数据新鲜度、最近一次回测的成本/PIT 口径、
 * 有没有过样本外验证），属于**研发期的周期性检查**，不是「今天做什么」。
 * 排在首页第 2 位时，把当日全景和热点简报都挤到了折线以下，已下移。
 * 复用 monitor dataflow 新鲜度 + 最近回测 detail 的 cost_profile/oos 徽章；
 * OOS/成本告警入口深链到 #btopt（回测复盘成本网格 / 滚动 OOS）。
 */
import { useEffect, useMemo, useState } from "react";
import EvalBadges from "./EvalBadges";
import { Card } from "./ui";
import {
  api,
  BacktestResult,
  CostProfile,
  DataflowReport,
  EvalOos,
  PeadPit,
  ValuationPit,
} from "../api/client";
import { ThemeColors } from "../theme";

type AlertItem = {
  level: "warn" | "info";
  text: string;
  tab: "btopt" | "datasys";
  action: string;
};

function daysAgoShanghai(d: string | null | undefined): number | null {
  if (!d) return null;
  const t = new Date(`${d}T00:00:00+08:00`).getTime();
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86400000);
}

function freshTone(
  ago: number | null,
  colors: ThemeColors,
): string {
  if (ago == null) return colors.muted;
  if (ago <= 1) return colors.down;
  if (ago <= 3) return "#e8a520";
  return colors.up;
}

function freshLabel(ago: number | null, latest: string | null | undefined): string {
  if (ago == null) return latest ? String(latest) : "无数据";
  if (ago === 0) return "今日";
  if (ago > 0) return `${ago}天前`;
  return latest ? String(latest) : "无数据";
}

/** 由最近评估徽章字段推导入口提示（纯函数，便于单测/快照对照）。 */
export function deriveRobustnessAlerts(args: {
  klineLatest?: string | null;
  factorLatest?: string | null;
  cost_profile?: CostProfile | null;
  oos?: EvalOos | null;
  hasEval: boolean;
}): AlertItem[] {
  const out: AlertItem[] = [];
  const kAgo = daysAgoShanghai(args.klineLatest ?? null);
  const fAgo = daysAgoShanghai(args.factorLatest ?? null);
  if ((kAgo != null && kAgo > 3) || (fAgo != null && fAgo > 3)) {
    out.push({
      level: "warn",
      text: "行情/因子数据滞后超过 3 个交易日口径的自然日，请先核对数据中心新鲜度",
      tab: "datasys",
      action: "查看数据中心 →",
    });
  }

  if (!args.hasEval) {
    out.push({
      level: "info",
      text: "暂无回测评估记录；到「回测寻优」跑一次后，这里会显示成本/OOS 徽章摘要",
      tab: "btopt",
      action: "去回测寻优 →",
    });
    return out;
  }

  const v = args.oos?.verdict;
  if (v === "过拟合" || v === "未通过" || v === "样本不足") {
    out.push({
      level: "warn",
      text: `最近评估 OOS·${v}——建议在回测复盘跑「滚动验证」核对多窗稳定性`,
      tab: "btopt",
      action: "打开滚动 OOS →",
    });
  } else if (args.oos?.enabled === false || (!args.oos && args.hasEval)) {
    out.push({
      level: "info",
      text: "最近回测未带 OOS 门禁；可在回测复盘启用滚动验证",
      tab: "btopt",
      action: "打开滚动 OOS →",
    });
  } else if (args.oos?.fragile_regime === true) {
    out.push({
      level: "warn",
      text: "滚动验证标记脆弱 regime——跨窗结论不一致，建议复核",
      tab: "btopt",
      action: "打开滚动 OOS →",
    });
  }

  const src = args.cost_profile?.source;
  if (src === "grid_pick") {
    out.push({
      level: "info",
      text: "成本口径来自网格选中（grid_pick）；可回成本敏感网格对照 ×1/×2",
      tab: "btopt",
      action: "打开成本网格 →",
    });
  } else if (args.cost_profile) {
    out.push({
      level: "info",
      text: "可用成本敏感网格扫描佣金×滑点，筛掉低摩擦才成立的 alpha",
      tab: "btopt",
      action: "打开成本网格 →",
    });
  }

  return out;
}

function goTab(tab: string) {
  window.dispatchEvent(new CustomEvent("quant-nav", { detail: { tab } }));
}

export default function TodayRobustnessCard({ colors }: { colors: ThemeColors }) {
  const [dataflow, setDataflow] = useState<DataflowReport | null>(null);
  const [detail, setDetail] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setErr(null);
      try {
        const dfP = api.monitorDataflow().catch(() => null);
        const histP = api.backtestHistory(5).catch(() => [] as BacktestResult[]);
        const [df, hist] = await Promise.all([dfP, histP]);
        if (cancelled) return;
        setDataflow(df);
        if (hist?.length) {
          try {
            const d = await api.backtestDetail(hist[0].id);
            if (!cancelled) setDetail(d);
          } catch {
            if (!cancelled) setDetail(null);
          }
        } else if (!cancelled) {
          setDetail(null);
        }
      } catch (e) {
        if (!cancelled) setErr((e as Error).message || "加载失败");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const klineLatest = (dataflow?.gold?.kline_latest as string | null | undefined) ?? null;
  const factorLatest = (dataflow?.gold?.factor_latest as string | null | undefined) ?? null;
  const klineRows = dataflow?.gold?.kline_daily as number | undefined;
  const factorRows = dataflow?.gold?.factor_daily as number | undefined;

  const cost_profile = (detail?.cost_profile ?? null) as CostProfile | null;
  const valuation_pit = (detail?.valuation_pit ?? null) as ValuationPit | null;
  const pead_pit = (detail?.pead_pit ?? null) as PeadPit | null;
  const oos = (detail?.oos ?? null) as EvalOos | null;

  const alerts = useMemo(
    () =>
      deriveRobustnessAlerts({
        klineLatest,
        factorLatest,
        cost_profile,
        oos,
        hasEval: !!detail,
      }),
    [klineLatest, factorLatest, cost_profile, oos, detail],
  );

  const kAgo = daysAgoShanghai(klineLatest);
  const fAgo = daysAgoShanghai(factorLatest);

  const evalTitle = detail
    ? `#${detail.id} · ${detail.strategy_key || "策略"} · ${detail.symbol || "—"}`
    : null;
  const evalRange = detail
    ? `${(detail as { start?: string; start_date?: string }).start
        || (detail as { start_date?: string }).start_date
        || "?"} ~ ${(detail as { end?: string; end_date?: string }).end
        || (detail as { end_date?: string }).end_date
        || "?"}`
    : null;

  return (
    <Card
      colors={colors}
      pad={12}
      title={
        <span>
          策略体检
          <span style={{ color: colors.muted, fontSize: 12, fontWeight: 400, marginLeft: 8 }}>
            信号基于多新的数据 · 最近一次回测的可信度 · 样本外与成本检验入口
          </span>
        </span>
      }
    >
      {loading && (
        <div style={{ color: colors.muted, fontSize: 13, padding: "4px 2px" }}>加载中…</div>
      )}
      {!loading && err && (
        <div style={{ color: colors.up, fontSize: 13 }}>{err}</div>
      )}
      {!loading && !err && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {/* —— 数据新鲜度 —— */}
          <div>
            <div style={{ fontSize: 12, color: colors.muted, marginBottom: 6 }}>数据新鲜度</div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              {[
                {
                  k: "行情K线",
                  latest: klineLatest,
                  ago: kAgo,
                  extra: klineRows != null ? `${Number(klineRows).toLocaleString()} 行` : null,
                },
                {
                  k: "因子截面",
                  latest: factorLatest,
                  ago: fAgo,
                  extra: factorRows != null ? `${Number(factorRows).toLocaleString()} 行` : null,
                },
              ].map((it) => (
                <div
                  key={it.k}
                  style={{
                    border: `1px solid ${colors.border}`,
                    borderRadius: 9,
                    padding: "8px 12px",
                    background: colors.card,
                  }}
                >
                  <div style={{ fontSize: 11, color: colors.muted }}>{it.k}</div>
                  <div
                    style={{
                      fontSize: 17,
                      fontWeight: 700,
                      color: freshTone(it.ago, colors),
                      fontVariantNumeric: "tabular-nums",
                    }}
                  >
                    {freshLabel(it.ago, it.latest)}
                  </div>
                  <div style={{ fontSize: 10.5, color: colors.muted }}>
                    {it.latest ?? "无数据"}
                    {it.extra ? ` · ${it.extra}` : ""}
                  </div>
                </div>
              ))}
            </div>
            {!dataflow && (
              <div style={{ fontSize: 12, color: colors.muted, marginTop: 6 }}>
                新鲜度接口暂不可用（未伪造日期）
              </div>
            )}
          </div>

          {/* —— 最近评估徽章 —— */}
          <div>
            <div style={{ fontSize: 12, color: colors.muted, marginBottom: 6 }}>最近评估徽章</div>
            {detail ? (
              <div
                style={{
                  border: `1px solid ${colors.border}`,
                  borderRadius: 9,
                  padding: "10px 12px",
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                }}
              >
                <div style={{ fontSize: 13, fontWeight: 600 }}>
                  {evalTitle}
                  <span style={{ color: colors.muted, fontWeight: 400, marginLeft: 8, fontSize: 12 }}>
                    {evalRange}
                    {typeof detail.sharpe === "number" ? ` · Sharpe ${detail.sharpe.toFixed(2)}` : ""}
                  </span>
                </div>
                <EvalBadges
                  cost_profile={cost_profile}
                  valuation_pit={valuation_pit}
                  pead_pit={pead_pit}
                  oos={oos}
                  colors={colors}
                />
                {!cost_profile && !oos && !valuation_pit && !pead_pit && (
                  <div style={{ fontSize: 12, color: colors.muted }}>
                    该条回测尚未写入评估徽章字段（多为徽章上线前的旧记录）
                  </div>
                )}
              </div>
            ) : (
              <div
                style={{
                  border: `1px dashed ${colors.border}`,
                  borderRadius: 9,
                  padding: "12px",
                  color: colors.muted,
                  fontSize: 12.5,
                }}
              >
                暂无回测历史——不会编造徽章。完成一次回测后自动出现成本/OOS 摘要。
              </div>
            )}
          </div>

          {/* —— OOS / 成本告警入口 —— */}
          <div>
            <div style={{ fontSize: 12, color: colors.muted, marginBottom: 6 }}>OOS / 成本入口</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {alerts.map((a, i) => (
                <button
                  key={`${a.text}-${i}`}
                  type="button"
                  onClick={() => goTab(a.tab)}
                  style={{
                    textAlign: "left",
                    cursor: "pointer",
                    border: `1px solid ${a.level === "warn" ? "#e8a52088" : colors.border}`,
                    background: a.level === "warn" ? "#e8a52012" : colors.card,
                    borderRadius: 9,
                    padding: "9px 12px",
                    color: colors.text,
                    fontSize: 12.5,
                    lineHeight: 1.55,
                  }}
                >
                  <span style={{ color: a.level === "warn" ? "#c8860d" : colors.muted, fontWeight: 600 }}>
                    {a.level === "warn" ? "⚠ " : "→ "}
                  </span>
                  {a.text}
                  <span style={{ float: "right", color: colors.accent, fontWeight: 600 }}>{a.action}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}
