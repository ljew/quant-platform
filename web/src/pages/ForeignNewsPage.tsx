/**
 * 境外资讯（OPS-FN-1a）：RSS + 可选 X 日爬入库。
 *
 * 独立于「全部资讯」：源状态条 + 芯片多选 + 时间窗 + 关键词 + 立即同步。
 * 本页以列表为主；无 LLM 时明示「仅列表不总结」（OPS-FN-1b）。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Btn, Card, PageHeader } from "../components/ui";
import { newsApi, ForeignSourceStatus, NewsFeedItem } from "../api/client";
import { useTheme } from "../theme";

const PAGE = 60;

const RANGES: { label: string; hours: number }[] = [
  { label: "24 小时", hours: 24 },
  { label: "72 小时", hours: 72 },
  { label: "7 天", hours: 168 },
];

/** status → 芯片色 */
const STATUS_COLOR: Record<string, string> = {
  ok: "#1a8a48",
  fail: "#c0392b",
  degraded: "#b9770e",
  unconfigured: "#7f8c8d",
  disabled: "#95a5a6",
};

function fmtTime(s: string | null): string {
  if (!s) return "—";
  const d = new Date(s);
  const now = new Date();
  const p2 = (n: number) => String(n).padStart(2, "0");
  const hm = `${p2(d.getHours())}:${p2(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return hm;
  return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${hm}`;
}

export default function ForeignNewsPage() {
  const { colors } = useTheme();
  const [sources, setSources] = useState<ForeignSourceStatus[]>([]);
  const [active, setActive] = useState<string[]>([]); // 多选；空=全部启用源
  const [hours, setHours] = useState(24);
  const [q, setQ] = useState("");
  const [qInput, setQInput] = useState("");
  const [items, setItems] = useState<NewsFeedItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState("");
  const [schedHint, setSchedHint] = useState("");

  const loadSources = useCallback(() => {
    newsApi
      .foreignSources(hours)
      .then(setSources)
      .catch(() => setSources([]));
    newsApi
      .foreignSyncStatus()
      .then((s) => {
        const slots = (s.slots || []).join(" / ");
        const next = s.next_slot ? `下一槽 ${s.next_slot}` : "";
        const last = s.last_run_at ? `上次 ${s.last_run_at}` : "尚未跑过";
        setSchedHint(
          `调度 ${s.enabled ? "开" : "关"} · 槽 ${slots}（Asia/Shanghai）· ${last}` +
            (next ? ` · ${next}` : "") +
            (s.last_result ? ` · ${s.last_result}` : ""),
        );
      })
      .catch(() => setSchedHint(""));
  }, [hours]);

  const sourcesParam = useMemo(
    () => (active.length ? active.join(",") : undefined),
    [active],
  );

  const load = useCallback(
    (offset: number) => {
      setLoading(true);
      newsApi
        .foreignItems({ sources: sourcesParam, q, hours, limit: PAGE, offset })
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
    [sourcesParam, q, hours],
  );

  useEffect(() => {
    loadSources();
  }, [loadSources]);

  useEffect(() => {
    load(0);
  }, [load]);

  const toggleSource = (key: string) => {
    setActive((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  };

  const doSync = async () => {
    setSyncing(true);
    setSyncMsg("");
    try {
      const res = await newsApi.foreignSync();
      const added = Object.values(res).reduce(
        (s, v) => s + (typeof v?.added === "number" && v.added > 0 ? v.added : 0),
        0,
      );
      const bad = Object.entries(res)
        .filter(([, v]) => v && (v.status === "fail" || v.status === "degraded"))
        .map(([k]) => k);
      const unconf = Object.entries(res)
        .filter(([, v]) => v && v.status === "unconfigured")
        .map(([k]) => k);
      let msg = `同步完成 +${added} 条`;
      if (unconf.length) msg += `；${unconf.join(",")} 未配置`;
      if (bad.length) msg += `；异常 ${bad.join(",")}`;
      setSyncMsg(msg);
      loadSources();
      load(0);
    } catch (e) {
      setSyncMsg(`同步失败：${(e as Error).message || e}`);
    } finally {
      setSyncing(false);
    }
  };

  const enabledSources = useMemo(
    () => sources.filter((s) => s.enabled || s.status !== "disabled"),
    [sources],
  );

  return (
    <div style={{ maxWidth: 1180, margin: "0 auto" }}>
      <PageHeader
        title="境外资讯"
        desc={
          schedHint ||
          "RSS（默认 8 源）+ 可选 X · 三槽 07:00 / 12:30 / 21:00 Asia/Shanghai"
        }
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
                width: 220,
                padding: "6px 10px",
                borderRadius: 8,
                border: `1px solid ${colors.border}`,
                background: colors.card,
                color: colors.text,
                fontSize: 12.5,
              }}
            />
            {q && (
              <Btn
                small
                onClick={() => {
                  setQ("");
                  setQInput("");
                }}
              >
                清除
              </Btn>
            )}
            <Btn small onClick={doSync} disabled={syncing}>
              {syncing ? "同步中…" : "立即同步"}
            </Btn>
          </span>
        }
      />

      {/* —— 源状态条 —— */}
      <div
        style={{
          display: "flex",
          gap: 8,
          flexWrap: "wrap",
          alignItems: "center",
          marginBottom: 10,
        }}
      >
        {sources.map((s) => {
          const c = STATUS_COLOR[s.status] || colors.muted;
          const tip = [
            `状态 ${s.status}`,
            s.last_ok_at ? `上次成功 ${s.last_ok_at}` : "",
            `上次新增 ${s.last_count ?? 0}`,
            s.count != null ? `近窗 ${s.count} 条` : "",
            s.last_error || "",
            s.source === "fn_x" && s.configured === false
              ? "未配置 X API，RSS 源仍可用"
              : "",
          ]
            .filter(Boolean)
            .join(" · ");
          return (
            <span
              key={s.source}
              title={tip}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "3px 10px",
                borderRadius: 999,
                fontSize: 12,
                border: `1px solid ${c}55`,
                background: `${c}14`,
                color: c,
                opacity: s.status === "disabled" ? 0.55 : 1,
              }}
            >
              <span
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: 99,
                  background: c,
                  display: "inline-block",
                }}
              />
              {s.label}
              <span style={{ opacity: 0.75 }}>{s.count ?? 0}</span>
            </span>
          );
        })}
      </div>

      {syncMsg && (
        <div
          style={{
            marginBottom: 10,
            fontSize: 12.5,
            color: colors.muted,
          }}
        >
          {syncMsg}
        </div>
      )}

      {/* —— 筛选：源芯片多选 + 时间 —— */}
      <div
        style={{
          display: "flex",
          gap: 8,
          flexWrap: "wrap",
          alignItems: "center",
          marginBottom: 12,
        }}
      >
        <button
          onClick={() => setActive([])}
          style={{
            padding: "4px 12px",
            borderRadius: 999,
            cursor: "pointer",
            fontSize: 12.5,
            border: `1px solid ${active.length === 0 ? colors.accent : colors.border}`,
            background: active.length === 0 ? `${colors.accent}18` : colors.card,
            color: active.length === 0 ? colors.accent : colors.text,
            fontWeight: active.length === 0 ? 600 : 400,
          }}
        >
          全部
        </button>
        {enabledSources
          .filter((s) => s.status !== "disabled")
          .map((s) => {
            const on = active.includes(s.source);
            const c = STATUS_COLOR[s.status] || colors.muted;
            return (
              <button
                key={s.source}
                onClick={() => toggleSource(s.source)}
                title={s.label}
                style={{
                  padding: "4px 12px",
                  borderRadius: 999,
                  cursor: "pointer",
                  fontSize: 12.5,
                  border: `1px solid ${on ? c : colors.border}`,
                  background: on ? `${c}18` : colors.card,
                  color: on ? c : colors.text,
                  fontWeight: on ? 600 : 400,
                }}
              >
                {s.label}
              </button>
            );
          })}
        <span style={{ flex: 1 }} />
        <select
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
          style={{
            padding: "5px 10px",
            borderRadius: 8,
            border: `1px solid ${colors.border}`,
            background: colors.card,
            color: colors.text,
            fontSize: 12.5,
          }}
        >
          {RANGES.map((r) => (
            <option key={r.hours} value={r.hours}>
              {r.label}
            </option>
          ))}
        </select>
      </div>

      <Card colors={colors} pad={0}>
        <div
          style={{
            padding: "8px 14px",
            borderBottom: `1px solid ${colors.border}`,
            fontSize: 12,
            color: colors.muted,
          }}
        >
          共 {total} 条
          {active.length
            ? `（已选 ${active.length} 源）`
            : ""}
          {q ? ` · 关键词「${q}」` : ""}
          {loading ? " · 加载中…" : ""}
        </div>

        {items.map((it) => {
          const c =
            STATUS_COLOR[
              sources.find((s) => s.source === it.source)?.status || "ok"
            ] || colors.muted;
          return (
            <div
              key={it.id}
              style={{
                padding: "10px 14px",
                borderBottom: `1px solid ${colors.border}`,
                display: "flex",
                gap: 12,
                alignItems: "flex-start",
              }}
            >
              <div
                style={{
                  width: 78,
                  flexShrink: 0,
                  fontSize: 11.5,
                  color: colors.muted,
                  fontVariantNumeric: "tabular-nums",
                  paddingTop: 1,
                }}
              >
                {fmtTime(it.published_at)}
              </div>
              <div style={{ width: 110, flexShrink: 0, paddingTop: 1 }}>
                <Badge text={it.source_label || it.source} color={c} soft />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.5 }}>
                  {it.url ? (
                    <a
                      href={it.url}
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: colors.text, textDecoration: "none" }}
                    >
                      {it.title}{" "}
                      <span style={{ color: colors.accent, fontSize: 11 }}>↗</span>
                    </a>
                  ) : (
                    it.title
                  )}
                </div>
                {it.summary && (
                  <div
                    style={{
                      fontSize: 11.5,
                      color: colors.muted,
                      marginTop: 3,
                      lineHeight: 1.6,
                      display: "-webkit-box",
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: "vertical",
                      overflow: "hidden",
                    }}
                  >
                    {it.summary}
                  </div>
                )}
              </div>
            </div>
          );
        })}

        {!items.length && !loading && (
          <div
            style={{
              padding: 28,
              textAlign: "center",
              color: colors.muted,
              fontSize: 13,
            }}
          >
            暂无境外资讯 —— 点右上「立即同步」拉取 RSS
            {sources.some((s) => s.source === "fn_x" && s.status === "unconfigured")
              ? "（X 未配置 Bearer，不影响 RSS）"
              : ""}
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

      <div
        style={{
          marginTop: 10,
          fontSize: 11.5,
          color: colors.muted,
          lineHeight: 1.7,
        }}
      >
        源状态条颜色：绿=ok / 红=fail / 黄=degraded / 灰=unconfigured 或 disabled。
        调度每日三槽（07:00 / 12:30 / 21:00 Asia/Shanghai）；也可手动「立即同步」。
        未配置 LLM，仅列表不总结；无 X Bearer 时 RSS 境外块仍可用。
        热点简报 / 正式报告会纳入本页入库的境外条（编号 O*）。
      </div>
    </div>
  );
}
