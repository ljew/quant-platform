/**
 * 全部资讯：各来源原文归一在一页里看。
 *
 * 分工：
 *   本页 = 原始信息（不做判断，只做归一 + 筛选 + 检索）
 *   首页「热点简报」= 对原始信息做筛选汇总后的判断
 *
 * 来源：财联社 / 东财 / 新浪（实时流）+ tushare / 公众号 / 股吧（按日/按小时同步）
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Btn, Card, PageHeader } from "../components/ui";
import { newsApi, NewsFeedItem, NewsSourceStat } from "../api/client";
import { useTheme } from "../theme";

const PAGE = 60;

/** 多久内入库算「新」。首页原来的「本轮新增」块撤掉后，改在这里标。 */
const NEW_WINDOW_MIN = 90;

/** 库里的 fetched_at 是本地时间且无时区标记，直接 new Date 即本地语义。 */
function isNew(it: NewsFeedItem): boolean {
  const raw = it.fetched_at;
  if (!raw) return false;
  const t = new Date(raw.includes("T") ? raw : raw.replace(" ", "T")).getTime();
  return Number.isFinite(t) && Date.now() - t < NEW_WINDOW_MIN * 60 * 1000;
}

const RANGES: { label: string; hours: number }[] = [
  { label: "24 小时", hours: 24 },
  { label: "3 天", hours: 72 },
  { label: "7 天", hours: 168 },
  { label: "30 天", hours: 720 },
  { label: "全部", hours: 0 },
];

/** 各来源一个固定色，扫一眼就能分清是谁家的 */
const SOURCE_COLOR: Record<string, string> = {
  cls: "#c0392b",
  eastmoney: "#b9770e",
  sina: "#8e44ad",
  tushare: "#16688a",
  deepseek: "#4d5bd0",
  wechat: "#1a8a48",
  guba: "#5d6d7e",
  // 券商研报：靛蓝，与「东财」的棕黄拉开 —— 两者内容不同（研报是观点，
  // 东财是新闻），颜色太近会让人以为是一类东西。
  research: "#2c6e9b",
};

function fmtTime(s: string | null): string {
  if (!s) return "—";
  // 今天的只显示时分，其余显示月-日 时:分
  const d = new Date(s);
  const now = new Date();
  const p2 = (n: number) => String(n).padStart(2, "0");
  const hm = `${p2(d.getHours())}:${p2(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return hm;
  return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${hm}`;
}

export default function NewsFeedPage() {
  const { colors } = useTheme();
  const [sources, setSources] = useState<NewsSourceStat[]>([]);
  const [active, setActive] = useState<string>("");        // "" = 全部
  const [hours, setHours] = useState(72);
  const [q, setQ] = useState("");
  const [qInput, setQInput] = useState("");
  const [items, setItems] = useState<NewsFeedItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);

  // 计数必须跟随时间范围，否则顶部「全部 1387」和列表「共 1102 条」会自相矛盾
  const loadSources = useCallback(() => {
    newsApi
      .feedSources(hours)
      .then(setSources)
      .catch(() => setSources([]));
  }, [hours]);

  const load = useCallback(
    (offset: number) => {
      setLoading(true);
      newsApi
        .feedItems({ sources: active, q, hours, limit: PAGE, offset })
        .then((d) => {
          setTotal(d.total);
          setItems((prev) => (offset === 0 ? d.items : [...prev, ...d.items]));
        })
        .catch(() => {
          if (offset === 0) {
            setItems([]);
            setTotal(0);
          }
        })
        .finally(() => setLoading(false));
    },
    [active, q, hours],
  );

  useEffect(() => {
    loadSources();
  }, [loadSources]);

  useEffect(() => {
    load(0);
  }, [load]);

  const doSync = async () => {
    setSyncing(true);
    try {
      await newsApi.feedSync();
      loadSources();
      load(0);
    } finally {
      setSyncing(false);
    }
  };

  const totalAll = useMemo(
    () => sources.reduce((s, x) => s + x.count, 0),
    [sources],
  );

  return (
    <div style={{ maxWidth: 1180, margin: "0 auto" }}>
      <PageHeader
        title="全部资讯"
        desc={`${sources.length} 个来源 · ${totalAll} 条原文 · 不做判断，只做归一与检索（判断见首页「热点简报」）`}
        actions={
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input
              value={qInput}
              onChange={(e) => setQInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") setQ(qInput.trim());
              }}
              placeholder="搜标题 / 摘要，回车"
              style={{
                width: 220, padding: "6px 10px", borderRadius: 8,
                border: `1px solid ${colors.border}`, background: colors.card,
                color: colors.text, fontSize: 12.5,
              }}
            />
            {q && (
              <Btn small onClick={() => { setQ(""); setQInput(""); }}>清除</Btn>
            )}
            <Btn small onClick={doSync} disabled={syncing}>
              {syncing ? "同步中…" : "立即同步"}
            </Btn>
          </span>
        }
      />

      {/* —— 来源筛选 + 时间范围 —— */}
      <div
        style={{
          display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center",
          marginBottom: 12,
        }}
      >
        <button
          onClick={() => setActive("")}
          style={{
            padding: "4px 12px", borderRadius: 999, cursor: "pointer", fontSize: 12.5,
            border: `1px solid ${active === "" ? colors.accent : colors.border}`,
            background: active === "" ? `${colors.accent}18` : colors.card,
            color: active === "" ? colors.accent : colors.text,
            fontWeight: active === "" ? 600 : 400,
          }}
        >
          全部 {totalAll}
        </button>
        {sources.map((s) => {
          const on = active === s.key;
          const c = SOURCE_COLOR[s.key] ?? colors.muted;
          return (
            <button
              key={s.key}
              onClick={() => setActive(on ? "" : s.key)}
              title={s.latest ? `最新：${s.latest}` : "暂无数据"}
              style={{
                padding: "4px 12px", borderRadius: 999, cursor: "pointer", fontSize: 12.5,
                border: `1px solid ${on ? c : colors.border}`,
                background: on ? `${c}18` : colors.card,
                color: on ? c : colors.text,
                fontWeight: on ? 600 : 400,
              }}
            >
              {s.label} {s.count}
            </button>
          );
        })}
        <span style={{ flex: 1 }} />
        <select
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
          style={{
            padding: "5px 10px", borderRadius: 8, border: `1px solid ${colors.border}`,
            background: colors.card, color: colors.text, fontSize: 12.5,
          }}
        >
          {RANGES.map((r) => (
            <option key={r.hours} value={r.hours}>{r.label}</option>
          ))}
        </select>
      </div>

      <Card colors={colors} pad={0}>
        <div style={{ padding: "8px 14px", borderBottom: `1px solid ${colors.border}`,
                      fontSize: 12, color: colors.muted }}>
          共 {total} 条{active ? `（来源：${sources.find((s) => s.key === active)?.label ?? active}）` : ""}
          {q ? ` · 关键词「${q}」` : ""}
          {loading ? " · 加载中…" : ""}
        </div>

        {items.map((it) => {
          const c = SOURCE_COLOR[it.source] ?? colors.muted;
          return (
            <div
              key={it.id}
              style={{
                padding: "10px 14px", borderBottom: `1px solid ${colors.border}`,
                display: "flex", gap: 12, alignItems: "flex-start",
              }}
            >
              <div style={{ width: 78, flexShrink: 0, fontSize: 11.5, color: colors.muted,
                            fontVariantNumeric: "tabular-nums", paddingTop: 1 }}>
                {fmtTime(it.published_at)}
              </div>
              <div style={{ width: 74, flexShrink: 0, paddingTop: 1 }}>
                <Badge text={it.source_label || it.source} color={c} soft />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.5 }}>
                  {isNew(it) && (
                    <span style={{
                      display: "inline-block", fontSize: 10, lineHeight: "15px",
                      padding: "0 4px", marginRight: 5, borderRadius: 3,
                      background: `${colors.accent}18`, color: colors.accent,
                      fontWeight: 600, verticalAlign: "1px",
                    }}>新</span>
                  )}
                  {it.url ? (
                    <a href={it.url} target="_blank" rel="noreferrer"
                       style={{ color: colors.text, textDecoration: "none" }}>
                      {it.title} <span style={{ color: colors.accent, fontSize: 11 }}>↗</span>
                    </a>
                  ) : (
                    it.title
                  )}
                  {(it.dup_count ?? 1) >= 2 && (
                    <span
                      title={`同题报道：${(it.dup_sources || []).join(" / ") || "多个来源"}`}
                      style={{
                        display: "inline-block", fontSize: 10, lineHeight: "15px",
                        padding: "0 5px", marginLeft: 6, borderRadius: 3,
                        cursor: "help", fontWeight: 600, verticalAlign: "1px",
                        // 2~3 家用中性色，4 家以上用暖色 —— 报道越多越值得看
                        background: (it.dup_count ?? 0) >= 4 ? "#b9770e18" : `${colors.muted}18`,
                        color: (it.dup_count ?? 0) >= 4 ? "#b9770e" : colors.muted,
                      }}>
                      {it.dup_count} 家报道
                    </span>
                  )}
                </div>
                {it.summary && (
                  <div style={{
                    fontSize: 11.5, color: colors.muted, marginTop: 3, lineHeight: 1.6,
                    display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical",
                    overflow: "hidden",
                  }}>
                    {it.summary}
                  </div>
                )}
                {it.symbols && (
                  <div style={{ fontSize: 11, color: colors.muted, marginTop: 2 }}>
                    代码 {it.symbols}
                  </div>
                )}
              </div>
            </div>
          );
        })}

        {!items.length && !loading && (
          <div style={{ padding: 28, textAlign: "center", color: colors.muted, fontSize: 13 }}>
            没有匹配的资讯 —— 换个来源/时间范围，或点右上「立即同步」
          </div>
        )}

        {items.length < total && (
          <div style={{ padding: 12, textAlign: "center" }}>
            <Btn small onClick={() => load(items.length)} disabled={loading}>
              {loading ? "加载中…" : `加载更多（还有 ${total - items.length} 条）`}
            </Btn>
          </div>
        )}
      </Card>

      <div style={{ marginTop: 10, fontSize: 11.5, color: colors.muted, lineHeight: 1.7 }}>
        来源标签上的数字 = 当前时间范围内的条数。财联社 / 东财 / 新浪每 10 分钟同步；
        tushare / 公众号 / 股吧每小时顺带同步
        （公众号每天 06:20、20:40 采集，tushare 与股吧由 ETL 每日落盘）。
        摘要为各来源原文前几百字，点标题看原文。
      </div>
    </div>
  );
}
