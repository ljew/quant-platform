import { useCallback, useEffect, useState } from "react";

import { Btn, Card, PageHeader } from "../components/ui";
import { NewsFeed, NewsItem, StockNews, newsApi } from "../api/client";
import { useTheme } from "../theme";

/**
 * 资讯中心：东财要闻 / 新浪快讯 / 腾讯全球行情 / 雪球热榜 + 个股新闻公告。
 *
 * 数据源与每日信号邮件里的「资讯」板块同源（backend/app/services/news_agg.py），
 * 后端带 5 分钟 TTL 缓存 + 并发拉取，因此进页面是秒开的。
 */
export default function NewsPage() {
  const { colors } = useTheme();
  const [feed, setFeed] = useState<NewsFeed | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback((refresh = false) => {
    setBusy(true);
    newsApi
      .feed(refresh)
      .then((d) => {
        setFeed(d);
        setErr("");
      })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => {
    load(false);
  }, [load]);

  const sub = feed
    ? `${feed.generated_at}${feed.cached ? ` · 缓存 ${Math.round(feed.age_sec)}s 前` : " · 刚刚更新"}${
        feed.stale ? " · 上游暂不可达，展示上次结果" : ""
      }`
    : "加载中…";

  return (
    <div>
      <PageHeader
        title="资讯中心"
        desc={sub}
        actions={
          <Btn small onClick={() => load(true)} disabled={busy}>
            {busy ? "刷新中…" : "强制刷新"}
          </Btn>
        }
      />

      {err && (
        <div style={{ color: colors.up, fontSize: 13, marginBottom: 12 }}>
          加载失败：{err}
        </div>
      )}

      {/* —— 全球行情条 —— */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit,minmax(132px,1fr))",
          gap: 10,
          marginBottom: 14,
        }}
      >
        {(feed?.global_quotes ?? []).map((q) => (
          <div
            key={q.code}
            style={{
              background: colors.card,
              border: `1px solid ${colors.border}`,
              borderRadius: 10,
              padding: "9px 12px",
            }}
          >
            <div style={{ fontSize: 11.5, color: colors.muted }}>{q.name}</div>
            <div
              style={{
                fontSize: 16,
                fontWeight: 700,
                fontVariantNumeric: "tabular-nums",
                fontFamily: "'SF Mono', Menlo, Consolas, monospace",
                color: q.change_pct >= 0 ? colors.up : colors.down,
              }}
            >
              {q.price.toFixed(2)}
            </div>
            <div
              style={{
                fontSize: 11.5,
                color: q.change_pct >= 0 ? colors.up : colors.down,
              }}
            >
              {q.change_pct >= 0 ? "+" : ""}
              {q.change_pct.toFixed(2)}%
            </div>
          </div>
        ))}
        {feed && feed.global_quotes.length === 0 && (
          <div style={{ color: colors.muted, fontSize: 12.5 }}>全球行情暂不可用</div>
        )}
      </div>

      {/* —— 三栏：要闻 / 快讯 / 右侧（热榜 + 个股） —— */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 360px", gap: 14 }}>
        <Card title="东财要闻" colors={colors} pad={0}>
          <NewsList items={feed?.headlines ?? []} colors={colors} max={20} />
        </Card>

        <Card title="新浪财经快讯" colors={colors} pad={0}>
          <NewsList items={feed?.sina_roll ?? []} colors={colors} max={30} showTime />
        </Card>

        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <Card title="雪球热榜" colors={colors} pad={0}>
            <div style={{ maxHeight: 330, overflow: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                <tbody>
                  {(feed?.xueqiu_hot ?? []).map((h, i) => (
                    <tr
                      key={h.symbol + i}
                      style={{ borderBottom: `1px solid ${colors.border}` }}
                    >
                      <td style={{ padding: "6px 8px", color: colors.muted, width: 22 }}>
                        {i + 1}
                      </td>
                      <td style={{ padding: "6px 4px" }}>
                        <div>{h.name}</div>
                        <div style={{ fontSize: 10.5, color: colors.muted }}>
                          {h.symbol.toUpperCase()}
                          {h.rank_change ? ` · 排名${h.rank_change > 0 ? "↑" : "↓"}${Math.abs(h.rank_change)}` : ""}
                        </div>
                      </td>
                      <td
                        style={{
                          padding: "6px 8px",
                          textAlign: "right",
                          fontVariantNumeric: "tabular-nums",
                          color: h.change_pct >= 0 ? colors.up : colors.down,
                        }}
                      >
                        {h.price ? h.price.toFixed(2) : "—"}
                        <div style={{ fontSize: 10.5 }}>
                          {h.change_pct >= 0 ? "+" : ""}
                          {h.change_pct.toFixed(2)}%
                        </div>
                      </td>
                    </tr>
                  ))}
                  {feed && feed.xueqiu_hot.length === 0 && (
                    <tr>
                      <td style={{ padding: 12, color: colors.muted }}>暂不可用</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </Card>

          <StockNewsCard colors={colors} />
        </div>
      </div>
    </div>
  );
}

/** 新闻列表：标题点击新窗口打开原文，时间/来源在第二行。 */
function NewsList({
  items,
  colors,
  max,
  showTime,
}: {
  items: NewsItem[];
  colors: ReturnType<typeof useTheme>["colors"];
  max: number;
  showTime?: boolean;
}) {
  if (items.length === 0) {
    return (
      <div style={{ padding: 16, color: colors.muted, fontSize: 12.5 }}>暂不可用</div>
    );
  }
  return (
    <div style={{ maxHeight: 560, overflow: "auto" }}>
      {items.slice(0, max).map((it, i) => (
        <a
          key={`${it.title}-${i}`}
          href={it.url || "#"}
          target="_blank"
          rel="noreferrer"
          style={{
            display: "block",
            padding: "9px 14px",
            borderBottom: `1px solid ${colors.border}`,
            textDecoration: "none",
            color: colors.text,
            background: i % 2 ? colors.tableStripe : "transparent",
          }}
        >
          <div style={{ fontSize: 13, lineHeight: 1.45 }}>{it.title}</div>
          <div style={{ fontSize: 10.5, color: colors.muted, marginTop: 3 }}>
            {it.source}
            {showTime && it.time ? ` · ${it.time.replace("T", " ").slice(5, 16)}` : ""}
          </div>
        </a>
      ))}
    </div>
  );
}

/** 个股新闻 + 公告查询。 */
function StockNewsCard({ colors }: { colors: ReturnType<typeof useTheme>["colors"] }) {
  const [q, setQ] = useState("");
  const [data, setData] = useState<StockNews | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    const s = q.trim();
    if (!s) return;
    setBusy(true);
    try {
      setData(await newsApi.stock(s));
    } catch (e) {
      setData({ ok: false, error: (e as Error).message, items: [] });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="个股新闻 / 公告" colors={colors} pad={12}>
      <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="600519 / sh600519"
          style={{
            flex: 1,
            padding: "6px 9px",
            borderRadius: 8,
            border: `1px solid ${colors.border}`,
            background: colors.card,
            color: colors.text,
            fontSize: 12.5,
          }}
        />
        <Btn small onClick={run} disabled={busy}>
          {busy ? "查询中…" : "查询"}
        </Btn>
      </div>
      {data && !data.ok && (
        <div style={{ color: colors.up, fontSize: 12 }}>{data.error}</div>
      )}
      <div style={{ maxHeight: 230, overflow: "auto" }}>
        {(data?.items ?? []).map((it, i) => (
          <a
            key={`${it.title}-${i}`}
            href={it.url || "#"}
            target="_blank"
            rel="noreferrer"
            style={{
              display: "block",
              padding: "7px 2px",
              borderBottom: `1px solid ${colors.border}`,
              textDecoration: "none",
              color: colors.text,
            }}
          >
            <div style={{ fontSize: 12.5, lineHeight: 1.4 }}>{it.title}</div>
            <div style={{ fontSize: 10.5, color: colors.muted, marginTop: 2 }}>
              {it.source} · {(it.time || "").slice(0, 10)}
            </div>
          </a>
        ))}
        {data && data.ok && data.items.length === 0 && (
          <div style={{ color: colors.muted, fontSize: 12 }}>无相关资讯</div>
        )}
      </div>
    </Card>
  );
}
