import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Btn, Card, PageHeader } from "../components/ui";
import {
  WechatArticle,
  WechatArticleDetail,
  WechatMp,
  WechatStats,
  wechatApi,
} from "../api/client";
import { useTheme } from "../theme";

/**
 * 公众号阅读：左号列表 / 中文章流 / 右全文。
 *
 * 数据来自 knowledge.db（宿主机 crawl_content.py 抓的全文库），本页只读。
 * 检索分词说明：FTS5 用 trigram，≥3 字才走索引，1-2 字由后端自动回落 LIKE，
 * 所以「城投」也能搜到（数量可能比 3 字词更多，属正常）。
 *
 * 「今日」视图是每天的主要入口：默认打开今天，没数据（停更期）自动退到最近有数据的一天。
 */

const RANGES = [
  { key: 0, label: "全部" },
  { key: 1, label: "今天" },
  { key: 7, label: "近 7 天" },
  { key: 30, label: "近 30 天" },
] as const;

const PAGE = 30;

export default function WechatPage() {
  const { colors } = useTheme();

  const [stats, setStats] = useState<WechatStats | null>(null);
  const [mps, setMps] = useState<WechatMp[]>([]);
  const [items, setItems] = useState<WechatArticle[]>([]);
  const [total, setTotal] = useState(0);
  const [mpId, setMpId] = useState("");
  const [range, setRange] = useState<number>(0);
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [listBusy, setListBusy] = useState(false);
  const [err, setErr] = useState("");

  const [detail, setDetail] = useState<WechatArticleDetail | null>(null);
  const [activeId, setActiveId] = useState("");
  const [detailBusy, setDetailBusy] = useState(false);

  const [mpFilter, setMpFilter] = useState("");
  const reqSeq = useRef(0);

  // —— 初次加载：统计 + 号列表 ——
  useEffect(() => {
    wechatApi.stats().then(setStats).catch((e) => setErr(String(e)));
    wechatApi.mps(300).then((d) => setMps(d.items)).catch(() => {});
  }, []);

  // —— 文章流 ——
  const loadList = useCallback(
    (nextOffset: number, append: boolean) => {
      const seq = ++reqSeq.current;
      setListBusy(true);
      wechatApi
        .articles({ mp_id: mpId, q, since_days: range, limit: PAGE, offset: nextOffset })
        .then((d) => {
          if (seq !== reqSeq.current) return; // 丢弃过期响应
          setErr(d.error || (d.db_missing ? "未找到 knowledge.db" : ""));
          setTotal(d.total);
          setOffset(nextOffset);
          setItems((prev) => (append ? [...prev, ...d.items] : d.items));
        })
        .catch((e) => setErr((e as Error).message))
        .finally(() => setListBusy(false));
    },
    [mpId, q, range],
  );

  useEffect(() => {
    loadList(0, false);
  }, [loadList]);

  const openArticle = useCallback((id: string) => {
    setActiveId(id);
    setDetailBusy(true);
    wechatApi
      .article(id)
      .then(setDetail)
      .catch((e) => setErr((e as Error).message))
      .finally(() => setDetailBusy(false));
  }, []);

  // 首次进页面自动打开最新一篇：右侧不空着，打开就能读（仅一次，后续筛选不抢焦点）
  const autoOpened = useRef(false);
  useEffect(() => {
    if (!autoOpened.current && items.length > 0) {
      autoOpened.current = true;
      openArticle(items[0].id);
    }
  }, [items, openArticle]);

  const filteredMps = useMemo(() => {
    const k = mpFilter.trim().toLowerCase();
    if (!k) return mps;
    return mps.filter((m) => m.mp_name.toLowerCase().includes(k));
  }, [mps, mpFilter]);

  const curMpName = mpId ? mps.find((m) => m.mp_id === mpId)?.mp_name || "" : "";
  const stale = stats?.stale_days ?? 0;
  const unavailable = stats && stats.available === false;

  const sub = unavailable
    ? stats?.msg || "全文库不可用"
    : stats
      ? `${stats.n?.toLocaleString()} 篇 · ${stats.mps} 个号 · ${stats.first_date} 起 · 最新 ${stats.last_date}` +
        (stale > 3 ? `（已 ${Math.round(stale)} 天未更新）` : "")
      : "加载中…";

  // —— 渲染 ——
  return (
    <div>
      <PageHeader
        title="公众号"
        desc={sub}
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            {/* 扫码不在本平台内：登录态由上游 wewe-rss 持有（launchd 常驻 :4000） */}
            <a
              href="http://localhost:4000/dash"
              target="_blank"
              rel="noreferrer"
              style={{
                padding: "5px 12px", borderRadius: 8, fontSize: 12.5, textDecoration: "none",
                border: `1px solid ${colors.accent}`, color: colors.accent,
                background: `${colors.accent}12`, whiteSpace: "nowrap",
              }}
            >
              上游后台 · 扫码登录
            </a>
            <Btn small onClick={() => { setQ(""); setQInput(""); setMpId(""); setRange(0); loadList(0, false); }} disabled={listBusy}>
              {listBusy ? "刷新中…" : "重置"}
            </Btn>
          </div>
        }
      />

      {unavailable && (
        <Card colors={colors} style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 13.5, lineHeight: 1.8 }}>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>全文库还没就绪</div>
            <div style={{ color: colors.muted }}>
              {stats?.msg}
              <br />
              抓取命令：<code style={{ fontSize: 12.5 }}>cd backend/scripts/wechat &amp;&amp; python3 crawl_content.py --since-days 7</code>
            </div>
          </div>
        </Card>
      )}

      {/* —— 停更提示：把「去哪扫码」直接写出来 —— */}
      {stats?.available && stale > 3 && (
        <Card colors={colors} style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 13, lineHeight: 1.85 }}>
            <div style={{ fontWeight: 600, marginBottom: 4, color: colors.up }}>
              已 {Math.round(stale)} 天没有新文章 —— 上游登录态大概率失效了
            </div>
            <div style={{ color: colors.muted }}>
              本平台只负责「读 wewe-rss 抓到的库并整理展示」，<b>扫码登录在 wewe-rss 自己的后台</b>：
              <br />① 点右上角「上游后台 · 扫码登录」→ 打开 <code>http://localhost:4000/dash</code>
              <br />② 在「账号管理」里点「扫码添加」，用微信读书 App 扫码确认
              <br />③ 登录恢复后点「重置」；每日 08:20 / 20:40 的自动抓取会把积压文章补齐
            </div>
          </div>
        </Card>
      )}

      {/* —— KPI 条 —— */}
      {stats?.available && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 14 }}>
          {[
            { label: "全文总量", value: (stats.n ?? 0).toLocaleString(), unit: "篇", hint: `${Math.round((stats.total_chars ?? 0) / 1e4).toLocaleString()} 万字` },
            { label: "覆盖公众号", value: String(stats.mps ?? 0), unit: "个", hint: "全部已抓正文" },
            { label: "近 7 天新发", value: String(stats.last7d ?? 0), unit: "篇", hint: (stats.last7d ?? 0) === 0 ? "停更期，需重新扫码" : "增量正常" },
            { label: "数据截止", value: (stats.last_date ?? "").slice(0, 10), unit: "", hint: stale > 3 ? `已 ${Math.round(stale)} 天未更新` : "较新" },
          ].map((k) => (
            <div key={k.label} style={{
              background: colors.card, border: `1px solid ${colors.border}`,
              borderRadius: 10, padding: "12px 14px",
            }}>
              <div style={{ fontSize: 11.5, color: colors.muted, letterSpacing: 0.4 }}>{k.label}</div>
              <div style={{ marginTop: 6, fontSize: 20, fontWeight: 700 }}>
                {k.value}<span style={{ fontSize: 12, fontWeight: 400, color: colors.muted, marginLeft: 4 }}>{k.unit}</span>
              </div>
              <div style={{ fontSize: 11, color: k.label === "数据截止" && stale > 3 ? colors.up : colors.muted, marginTop: 3 }}>{k.hint}</div>
            </div>
          ))}
        </div>
      )}

      {err && (
        <div style={{ fontSize: 12.5, color: colors.up, marginBottom: 10 }}>⚠ {err}</div>
      )}

      {/* —— 三栏主体 —— */}
      <div style={{ display: "grid", gridTemplateColumns: "186px minmax(0,1fr) minmax(0,1.35fr)", gap: 12, alignItems: "start" }}>

        {/* 左：公众号列表 */}
        <Card title="公众号" colors={colors} pad={0} style={{ maxHeight: "calc(100vh - 250px)", display: "flex", flexDirection: "column" }}>
          <div style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}` }}>
            <input
              value={mpFilter}
              onChange={(e) => setMpFilter(e.target.value)}
              placeholder="筛号名…"
              style={{
                width: "100%", boxSizing: "border-box", padding: "5px 8px", fontSize: 12,
                background: colors.bg, color: colors.text,
                border: `1px solid ${colors.border}`, borderRadius: 6, outline: "none",
              }}
            />
          </div>
          <div style={{ overflowY: "auto", flex: 1 }}>
            <button
              onClick={() => setMpId("")}
              style={mpRowStyle(!mpId, colors)}
            >
              <span style={{ fontWeight: !mpId ? 600 : 400 }}>全部公众号</span>
              <span style={{ fontSize: 11, color: colors.muted }}>{stats?.n?.toLocaleString() ?? ""}</span>
            </button>
            {filteredMps.map((m) => (
              <button key={m.mp_id} onClick={() => setMpId(m.mp_id)} style={mpRowStyle(mpId === m.mp_id, colors)}>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 120, textAlign: "left" }}>
                  {m.mp_name}
                </span>
                <span style={{ fontSize: 10.5, color: colors.muted, flexShrink: 0 }}>{m.n}</span>
              </button>
            ))}
            {filteredMps.length === 0 && (
              <div style={{ padding: 14, fontSize: 12, color: colors.muted }}>没匹配的号</div>
            )}
          </div>
        </Card>

        {/* 中：文章流 */}
        <Card pad={0} colors={colors} style={{ maxHeight: "calc(100vh - 250px)", display: "flex", flexDirection: "column" }}>
          <div style={{ padding: "10px 12px", borderBottom: `1px solid ${colors.border}`, display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ display: "flex", gap: 6 }}>
              <input
                value={qInput}
                onChange={(e) => setQInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { setQ(qInput.trim()); } }}
                placeholder="搜全文（≥3 字走索引，1-2 字自动 LIKE）"
                style={{
                  flex: 1, padding: "6px 9px", fontSize: 12.5,
                  background: colors.bg, color: colors.text,
                  border: `1px solid ${colors.border}`, borderRadius: 6, outline: "none",
                }}
              />
              <Btn small onClick={() => setQ(qInput.trim())} disabled={listBusy}>搜</Btn>
              {q && (
                <Btn small kind="ghost" onClick={() => { setQ(""); setQInput(""); }}>清除</Btn>
              )}
            </div>
            <div style={{ display: "flex", gap: 5, alignItems: "center", flexWrap: "wrap" }}>
              {RANGES.map((r) => (
                <button
                  key={r.key}
                  onClick={() => setRange(r.key)}
                  style={{
                    padding: "3px 9px", fontSize: 11.5, borderRadius: 999, cursor: "pointer",
                    border: `1px solid ${range === r.key ? colors.accent : colors.border}`,
                    background: range === r.key ? `${colors.accent}18` : "transparent",
                    color: range === r.key ? colors.accent : colors.muted,
                  }}
                >
                  {r.label}
                </button>
              ))}
              <span style={{ fontSize: 11.5, color: colors.muted, marginLeft: "auto" }}>
                {curMpName ? `· ${curMpName}` : ""} 共 {total.toLocaleString()} 篇
              </span>
            </div>
          </div>

          <div style={{ overflowY: "auto", flex: 1 }}>
            {items.length === 0 && !listBusy && (
              <div style={{ padding: 26, fontSize: 13, color: colors.muted, textAlign: "center", lineHeight: 1.9 }}>
                没有文章。
                <br />
                {range > 0 ? "该时间范围内无新发（可能停更），试试「全部」。" : "换个号或清掉关键词。"}
              </div>
            )}
            {items.map((a) => (
              <button
                key={a.id}
                onClick={() => openArticle(a.id)}
                style={{
                  display: "block", width: "100%", textAlign: "left", cursor: "pointer",
                  padding: "11px 13px",
                  background: activeId === a.id ? `${colors.accent}10` : "transparent",
                  borderLeft: `3px solid ${activeId === a.id ? colors.accent : "transparent"}`,
                  borderBottom: `1px solid ${colors.border}`,
                  border: 0, borderBottomWidth: 1, borderBottomStyle: "solid", borderBottomColor: colors.border,
                  color: colors.text,
                }}
              >
                <div style={{ fontSize: 13.5, fontWeight: 600, lineHeight: 1.45 }}>{a.title}</div>
                <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 5, display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ color: colors.accent }}>{a.mp_name}</span>
                  <span>{a.publish_date}</span>
                  <span>{a.chars.toLocaleString()} 字</span>
                </div>
                {a.digest && (
                  <div style={{
                    fontSize: 12, color: colors.muted, marginTop: 6, lineHeight: 1.6,
                    display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden",
                  }}>
                    {a.digest}
                  </div>
                )}
              </button>
            ))}
            {items.length < total && (
              <div style={{ padding: 12, textAlign: "center" }}>
                <Btn small kind="ghost" onClick={() => loadList(offset + PAGE, true)} disabled={listBusy}>
                  {listBusy ? "加载中…" : `加载更多（${items.length}/${total}）`}
                </Btn>
              </div>
            )}
          </div>
        </Card>

        {/* 右：全文 */}
        <Card
          title={detail ? detail.mp_name : "阅读"}
          extra={detail && (
            <a href={detail.url} target="_blank" rel="noreferrer"
               style={{ fontSize: 11.5, color: colors.accent, textDecoration: "none" }}>
              原文 ↗
            </a>
          )}
          colors={colors}
          pad={0}
          style={{ maxHeight: "calc(100vh - 250px)", display: "flex", flexDirection: "column" }}
        >
          {!detail && !detailBusy && (
            <div style={{ padding: 30, fontSize: 13, color: colors.muted, textAlign: "center", lineHeight: 2 }}>
              点左侧文章看全文
              <br />
              <span style={{ fontSize: 11.5 }}>共 {total.toLocaleString()} 篇可读</span>
            </div>
          )}
          {detailBusy && <div style={{ padding: 30, fontSize: 13, color: colors.muted, textAlign: "center" }}>加载中…</div>}
          {detail && !detailBusy && (
            <div style={{ overflowY: "auto", flex: 1, padding: "16px 20px 28px" }}>
              <h2 style={{ fontSize: 17.5, lineHeight: 1.5, margin: "0 0 10px", fontWeight: 700 }}>{detail.title}</h2>
              <div style={{ fontSize: 11.5, color: colors.muted, display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 14, paddingBottom: 12, borderBottom: `1px solid ${colors.border}` }}>
                <span style={{ color: colors.accent }}>{detail.mp_name}</span>
                {/* 抓取时 meta author 常常就是公众号名，重复显示没意义 */}
                {detail.author && detail.author !== detail.mp_name && <span>作者 {detail.author}</span>}
                <span>{detail.publish_date}</span>
                <span>{detail.content_text.length.toLocaleString()} 字</span>
              </div>
              <div style={{ fontSize: 14, lineHeight: 1.95, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                {detail.content_text || "（正文为空，可能是纯视频/图片文章）"}
              </div>
              <div style={{ marginTop: 18, paddingTop: 12, borderTop: `1px solid ${colors.border}`, fontSize: 11, color: colors.muted }}>
                抓取于 {detail.fetched_date} · 仅供个人研究，勿二次分发
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function mpRowStyle(active: boolean, colors: { accent: string; text: string; muted: string; border: string }): React.CSSProperties {
  return {
    display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6,
    width: "100%", padding: "7px 12px", fontSize: 12.5, cursor: "pointer",
    background: active ? `${colors.accent}12` : "transparent",
    color: active ? colors.accent : colors.text,
    border: 0, borderLeft: `2px solid ${active ? colors.accent : "transparent"}`,
    borderBottom: `1px solid ${colors.border}`,
    fontWeight: active ? 600 : 400,
  };
}
