/**
 * 归因分析（Sequoia signal_stats 迁移）：信号有效性六维统计。
 * 口径：T+1 开盘建仓、持有 T+N 收盘卖出，超额 = 个股 − 全市场等权。
 */
import { useEffect, useState } from "react";
import { Btn, Card, PageHeader } from "../components/ui";
import { get } from "../api/client";
import { useTheme } from "../theme";

interface StatRow {
  strategy?: string; streak_bucket?: string; reso_bucket?: string;
  tier?: string; month?: string;
  [k: string]: string | number | undefined;
}
interface StatsResult {
  ok: boolean; error?: string; window_days: number; n_signals: number; n_obs: number;
  generated_at: string;
  by_strategy: StatRow[]; by_streak: StatRow[]; by_resonance: StatRow[];
  by_tier: StatRow[]; by_month: StatRow[];
}
const HORIZONS = [1, 3, 5, 10, 20];
const STRATEGY_CN: Record<string, string> = {
  ma_volume: "均线量能", rps_breakout: "RPS强度突破", high_tight_flag: "高旗形整理",
  limit_up_shakeout: "涨停洗盘", uptrend_limit_down: "上升趋势跌停",
  turtle_trade: "海龟突破", dual_param: "双参数趋势波动", private_placement: "定增公告",
};

function Chg({ v }: { v: number | undefined }) {
  if (v === undefined || v === null) return <span>—</span>;
  const color = v >= 0 ? "#c0392b" : "#1e8449"; // 红涨绿跌
  return <span style={{ color, fontWeight: 600 }}>{v > 0 ? "+" : ""}{v.toFixed(2)}%</span>;
}

function StatsTable({ rows, keyLabel, keyOf }: {
  rows: StatRow[]; keyLabel: string; keyOf: (r: StatRow) => string;
}) {
  const { colors } = useTheme();
  return (
    <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 13 }}>
      <thead>
        <tr style={{ background: colors.tableStripe, textAlign: "left" }}>
          <th style={{ padding: "7px 10px" }}>{keyLabel}</th>
          {HORIZONS.map((n) => (
            <th key={n} style={{ padding: "7px 10px" }} colSpan={3}>T+{n}</th>
          ))}
        </tr>
        <tr style={{ color: colors.muted, fontSize: 11.5 }}>
          <th />
          {HORIZONS.map((n) => (
            <>
              <th key={`${n}n`} style={{ padding: "3px 10px" }}>样本</th>
              <th key={`${n}e`} style={{ padding: "3px 10px" }}>超额</th>
              <th key={`${n}w`} style={{ padding: "3px 10px" }}>胜率</th>
            </>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} style={{ borderTop: `1px solid ${colors.border}` }}>
            <td style={{ padding: "7px 10px", fontWeight: 600 }}>{keyOf(r)}</td>
            {HORIZONS.map((n) => (
              <>
                <td key={`${n}n`} style={{ padding: "7px 10px", color: colors.muted }}>{r[`n_${n}`] ?? "—"}</td>
                <td key={`${n}e`} style={{ padding: "7px 10px" }}><Chg v={r[`exc_${n}`] as number | undefined} /></td>
                <td key={`${n}w`} style={{ padding: "7px 10px" }}>{r[`win_${n}`] != null ? `${r[`win_${n}`]}%` : "—"}</td>
              </>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function AttributionPage() {
  const { colors } = useTheme();
  const [data, setData] = useState<StatsResult | null>(null);
  const [days, setDays] = useState(90);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = () => {
    setBusy(true);
    setError("");
    get<StatsResult>(`/signal/stats?days=${days}`).then(setData)
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };
  useEffect(load, [days]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <PageHeader
        title="归因分析"
        desc="信号有效性六维统计 · T+1 开盘建仓 / 持有 T+N 收盘 / 超额 vs 全市场等权"
        actions={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {[60, 90, 180].map((d) => (
              <Btn key={d} small kind={days === d ? "primary" : "ghost"}
                onClick={() => setDays(d)} disabled={busy}>{d} 日</Btn>
            ))}
            <Btn small onClick={load} disabled={busy}>刷新</Btn>
          </div>
        }
      />
      {error && <div style={{ color: colors.down, fontSize: 13 }}>{error}</div>}
      {data?.ok && (
        <div style={{ fontSize: 12.5, color: colors.muted }}>
          统计口径：{data.window_days} 日窗口 · {data.n_signals} 个信号 · {data.n_obs} 个收益观测 ·
          生成于 {data.generated_at}
        </div>
      )}
      {data?.ok && (
        <>
          <Card title="按策略（谁的信号在赚钱）" colors={colors}>
            <StatsTable rows={data.by_strategy} keyLabel="策略"
              keyOf={(r) => STRATEGY_CN[r.strategy || ""] || (r.strategy as string)} />
          </Card>
          <Card title="按连续天数（streak 越长越可靠？）" colors={colors}>
            <StatsTable rows={data.by_streak} keyLabel="连续出现" keyOf={(r) => r.streak_bucket || ""} />
          </Card>
          <Card title="按共振数（多策略共振是否更有效）" colors={colors}>
            <StatsTable rows={data.by_resonance} keyLabel="同日共振" keyOf={(r) => r.reso_bucket || ""} />
          </Card>
          <Card title="按质量分级" colors={colors}>
            <StatsTable rows={data.by_tier} keyLabel="分级" keyOf={(r) => r.tier || ""} />
          </Card>
          <Card title="按月（有没有翻脸）" colors={colors}>
            <StatsTable rows={data.by_month} keyLabel="月份" keyOf={(r) => r.month || ""} />
          </Card>
        </>
      )}
    </div>
  );
}
