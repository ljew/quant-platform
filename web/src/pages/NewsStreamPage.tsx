/**
 * 资讯流：一条时间倒序的流 + 顶部来源筛选器（设计文档 08 节要的形态）。
 *
 * 原来四个子页签（全部 / 境外 / 四时段报告 / 公众号）的差别只是「来源」和
 * 「加工深度」，不是用法 —— 查一条消息不该先做分类题。
 *
 * 三处必须说清的口径：
 * · 公众号取 knowledge.db 全文（7613 篇），不是 news_feed 里那 567 条摘要
 * · 时段报告是「加工过的观点」，混在快讯里但单独标色 —— 它和一条快讯不是一个东西
 * · 券商研报（research）也在流里，但它不是 alpha，只作参考
 */
import { useCallback, useEffect, useState } from "react";
import { newsApi, NewsStreamResp } from "../api/client";
import { useTheme } from "../theme";

const GROUP_META: Record<string, { cn: string; color: string }> = {
  domestic: { cn: "国内快讯", color: "#2471a3" },
  foreign: { cn: "境外", color: "#8e44ad" },
  wechat: { cn: "公众号", color: "#16a34a" },
  report: { cn: "时段报告", color: "#b9770e" },
};

const DAY_OPTS = [1, 2, 3, 7];

export default function NewsStreamPage() {
  const { colors } = useTheme();
  const [days, setDays] = useState(2);
  const [picked, setPicked] = useState<string[]>([]);
  const [data, setData] = useState<NewsStreamResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true); setErr("");
    newsApi.stream(days, picked)
      .then(setData)
      .catch((e) => setErr((e as Error).message))
      .finally(() => setLoading(false));
  }, [days, picked]);

  useEffect(() => { load(); }, [load]);

  const toggle = (g: string) =>
    setPicked((cur) => (cur.includes(g) ? cur.filter((x) => x !== g) : [...cur, g]));

  const chip = (on: boolean, color: string): React.CSSProperties => ({
    border: `1px solid ${on ? color : colors.border}`,
    background: on ? `${color}14` : "transparent",
    color: on ? color : colors.muted,
    borderRadius: 20, padding: "3px 12px", fontSize: 12, cursor: "pointer",
  });

  return (
    <div>
      {/* 筛选条 */}
      <div style={{
        display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
        padding: "0 2px 12px", borderBottom: `1px solid ${colors.border}`, marginBottom: 14,
      }}>
        <span style={{ fontSize: 12.5, fontWeight: 600 }}>资讯流</span>
        <span style={{ fontSize: 11.5, color: colors.muted }}>
          {data ? `${data.count} 条 · 近 ${data.days} 天` : ""}
          {loading ? " · 加载中…" : ""}
        </span>

        <span style={{ flex: 1 }} />

        {/* 时间范围 */}
        <div style={{ display: "flex", gap: 4 }}>
          {DAY_OPTS.map((d) => (
            <button key={d} onClick={() => setDays(d)} style={{
              border: `1px solid ${days === d ? colors.accent : colors.border}`,
              background: days === d ? `${colors.accent}14` : "transparent",
              color: days === d ? colors.accent : colors.muted,
              borderRadius: 6, padding: "2px 9px", fontSize: 11.5, cursor: "pointer",
            }}>{d} 天</button>
          ))}
        </div>

        {/* 来源筛选（多选，空=全部） */}
        <div style={{ display: "flex", gap: 6 }}>
          {Object.entries(GROUP_META).map(([k, m]) => (
            <button key={k} onClick={() => toggle(k)} style={chip(picked.includes(k), m.color)}>
              {m.cn}
              {data?.groups?.[k] ? <span style={{ opacity: 0.65 }}> {data.groups[k]}</span> : null}
            </button>
          ))}
        </div>

        <button onClick={load} style={{
          fontSize: 11.5, color: colors.accent, background: "none",
          border: `1px solid ${colors.border}`, borderRadius: 6, padding: "3px 10px", cursor: "pointer",
        }}>刷新</button>
      </div>

      {err && (
        <div style={{
          marginBottom: 12, padding: "10px 12px", borderRadius: 8, fontSize: 12.5,
          background: `${colors.up}12`, border: `1px solid ${colors.up}44`, color: colors.up,
        }}>{err}</div>
      )}

      {picked.length > 0 && (
        <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 10 }}>
          只看 {picked.map((k) => GROUP_META[k]?.cn ?? k).join(" / ")} ——
          <button
            onClick={() => setPicked([])}
            style={{ marginLeft: 6, fontSize: 11.5, color: colors.accent, background: "none", border: 0, cursor: "pointer" }}
          >看全部</button>
        </div>
      )}

      {/* 流 */}
      <div style={{ display: "flex", flexDirection: "column" }}>
        {(data?.items ?? []).map((it) => {
          const gm = GROUP_META[it.group] ?? { cn: it.group, color: colors.muted };
          const open = openId === it.id;
          return (
            <div
              key={it.id}
              onClick={() => setOpenId(open ? null : it.id)}
              style={{
                padding: "10px 4px", borderBottom: `1px solid ${colors.border}`, cursor: "pointer",
              }}
            >
              <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
                <span style={{
                  fontSize: 10.5, color: gm.color, border: `1px solid ${gm.color}55`,
                  background: `${gm.color}10`, borderRadius: 4, padding: "0 5px",
                  flexShrink: 0, lineHeight: "17px",
                }}>{gm.cn}</span>
                <span style={{
                  fontSize: 11.5, color: colors.muted, flexShrink: 0,
                  fontVariantNumeric: "tabular-nums", width: 84,
                }}>{it.at.slice(5)}</span>
                <span style={{ fontSize: 13, fontWeight: 600, flex: 1, minWidth: 0 }}>
                  {it.title}
                </span>
                <span style={{ fontSize: 11, color: colors.muted, flexShrink: 0 }}>
                  {it.source_cn}
                </span>
              </div>
              {it.summary && (
                <div style={{
                  marginTop: 4, marginLeft: 104, fontSize: 12, color: colors.muted, lineHeight: 1.75,
                  ...(open ? {} : { display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }),
                } as React.CSSProperties}>
                  {it.summary}
                </div>
              )}
              {open && it.url && (
                <div style={{ marginTop: 6, marginLeft: 104 }}>
                  <a
                    href={it.url}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    style={{ fontSize: 11.5, color: colors.accent, textDecoration: "none" }}
                  >打开原文 ↗</a>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {data && data.items.length === 0 && !loading && (
        <div style={{ padding: 30, textAlign: "center", color: colors.muted, fontSize: 13 }}>
          这 {days} 天没有符合条件的资讯 —— 换个范围或清掉筛选试试
        </div>
      )}

      {data && data.items.length > 0 && (
        <div style={{ marginTop: 10, fontSize: 11, color: colors.muted }}>
          共 {data.total} 条，显示前 {data.count} 条 · 点击条目展开摘要 ·
          公众号取全文库，时段报告是加工过的观点，别和快讯同等对待
        </div>
      )}
    </div>
  );
}
