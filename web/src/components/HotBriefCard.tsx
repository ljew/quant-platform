/**
 * 热点简报卡片（每小时 :02 生成，展示「今日累计」而不是单轮增量）。
 *
 * 为什么用 /today 而不是 /latest：
 *   每轮只报「本小时新增」，若卡片只显示最新一轮，就会出现 —— 这一小时全是
 *   DeepSeek 搜索条目、下一小时全是财联社快讯，用户看到的是「一会儿有一会儿没有」。
 *   今日累计把两边合并去重摆在一起，并标出每条首次出现时间。
 *
 * 三块内容：
 *   ① 综述   —— 350~1000 字，分政策/产业/海外/资金四段（每轮重写并覆盖当日整体）
 *   ② 今日主题 —— 各轮合并去重后的条目，带来源标签与首次出现时间
 *   ③ 原始输入 —— 本轮喂给模型的全部条目，● 采用 / ○ 丢弃
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Card } from "./ui";
import { api, HotBrief, HotBriefInput, HotBriefItem } from "../api/client";
import { useTheme } from "../theme";

const ITEMS_PREVIEW = 8;

/** 单条来源标签（可点击）。 */
function SourceTag({ item }: { item: HotBriefItem }) {
  const { colors } = useTheme();
  const isSearch = (item.source_kind || "").includes("搜索");
  const pill: React.CSSProperties = {
    fontSize: 10.5,
    fontWeight: 400,
    lineHeight: "16px",
    padding: "0 5px",
    borderRadius: 3,
    border: `1px solid ${colors.border}`,
    color: isSearch ? colors.accent : colors.muted,
    textDecoration: "none",
    whiteSpace: "nowrap",
  };
  const all = item.source_all?.length ? item.source_all : [item.source_label || ""];
  const tip =
    `来源：${all.filter(Boolean).join(" / ") || "未知"}` +
    (item.source_refs?.length ? `\n输入编号：${item.source_refs.join(", ")}` : "");

  const label = item.source_label || "未标注来源";
  return item.source_url ? (
    <a href={item.source_url} target="_blank" rel="noreferrer"
       title={`${tip} — 点击打开原文`} style={pill}>
      {label} ↗
    </a>
  ) : (
    <span title={tip} style={pill}>{label}</span>
  );
}

/** 快讯带具体时刻，搜索只有日期 —— 分开格式化，别统一成一种含糊写法。 */
function fmtInputTime(x: HotBriefInput): string {
  if (!x.ts) return "";
  const d = new Date(x.ts * 1000);
  const p2 = (n: number) => String(n).padStart(2, "0");
  return x.kind === "快讯"
    ? `${p2(d.getHours())}:${p2(d.getMinutes())}`
    : `${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
}

/** 可展开的「本轮原始输入」。● 表示被模型采用，○ 表示看过但丢弃。 */
function InputsPanel({ inputs, cited }: { inputs: HotBriefInput[]; cited: Set<string> }) {
  const { colors } = useTheme();
  const [open, setOpen] = useState(false);
  const used = inputs.filter((x) => cited.has(x.ref)).length;

  return (
    <div style={{ marginTop: 10, borderTop: `1px solid ${colors.border}`, paddingTop: 8 }}>
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          border: 0, background: "transparent", color: colors.accent, cursor: "pointer",
          fontSize: 11.5, padding: 0,
        }}
      >
        {open ? "▾" : "▸"} 本轮原始输入 {inputs.length} 条（已采用 {used} 条，其余被丢弃）
      </button>
      {open && (
        <div style={{ marginTop: 6, maxHeight: 260, overflowY: "auto" }}>
          {inputs.map((x) => {
            const on = cited.has(x.ref);
            const row = (
              <div style={{ display: "flex", gap: 8, alignItems: "baseline",
                            padding: "3px 0", fontSize: 11.5, opacity: on ? 1 : 0.55 }}>
                <span style={{ color: on ? colors.accent : colors.muted, width: 8 }}>
                  {on ? "●" : "○"}
                </span>
                <span style={{ color: colors.muted, width: 26, flexShrink: 0 }}>{x.ref}</span>
                <span style={{ color: colors.muted, width: 40, flexShrink: 0 }}>
                  {fmtInputTime(x)}
                </span>
                <span style={{ flex: 1, minWidth: 0, color: colors.text }}>
                  {x.title}{x.url ? " ↗" : ""}
                </span>
                <span style={{ color: colors.muted, flexShrink: 0 }}>{x.site}</span>
              </div>
            );
            return x.url ? (
              <a key={x.ref} href={x.url} target="_blank" rel="noreferrer"
                 style={{ textDecoration: "none", display: "block" }} title={x.title}>
                {row}
              </a>
            ) : (
              <div key={x.ref} title={x.title}>{row}</div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function HotBriefCard({
  columns = 1,
  dense = false,
}: {
  /** 首页用 2 列省纵向空间；因子实验室用 1 列留足影响路径的阅读宽度 */
  columns?: 1 | 2;
  dense?: boolean;
}) {
  const { colors } = useTheme();
  const [brief, setBrief] = useState<HotBrief | null>(null);
  const [loading, setLoading] = useState(true);
  const [showAllItems, setShowAllItems] = useState(false);
  const [summaryOpen, setSummaryOpen] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    api
      .hotBriefToday()
      // 后端无数据时返回 {}
      .then((d) => setBrief(d && (d.n_items || d.summary) ? d : null))
      .catch(() => setBrief(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const items = brief?.items || [];
  const paragraphs = useMemo(
    () => (brief?.summary || "").split(/\n+/).map((s) => s.trim()).filter(Boolean),
    [brief?.summary],
  );
  const shownItems = showAllItems ? items : items.slice(0, ITEMS_PREVIEW);
  const split = brief?.source_split || {};
  // 必须把「未标注」也算进来，否则 12+3 对不上总数 38，看着像丢数据。
  // 这些是加来源标注功能之前生成的旧条目，次日自然消失。
  const splitText = ["快讯", "搜索", "混合", "未标注"]
    .filter((k) => (split[k] || 0) > 0)
    .map((k) => `${k} ${split[k]}`)
    .join(" · ");

  const meta = brief ? (
    <span style={{ fontSize: 11.5, color: colors.muted, fontWeight: 400 }}>
      {brief.date || ""}
      {" · 共 "}
      {brief.n_briefs ?? 1} 轮 · {brief.n_items} 条
      {splitText ? `（${splitText}）` : ""}
      {brief.created_at ? ` · 最新 ${brief.created_at.slice(11, 16)}` : ""}
    </span>
  ) : null;

  return (
    <Card
      title="热点简报"
      colors={colors}
      extra={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
          {meta}
          <button
            onClick={load}
            disabled={loading}
            style={{
              border: `1px solid ${colors.border}`, background: colors.card,
              color: colors.muted, borderRadius: 4, padding: "1px 7px",
              fontSize: 11, cursor: loading ? "default" : "pointer",
            }}
          >
            {loading ? "…" : "刷新"}
          </button>
        </span>
      }
    >
      {!brief && (
        <div style={{ color: colors.muted, fontSize: 12.5, padding: "10px 2px" }}>
          {loading ? "加载中…" : "尚无简报 —— 调度器每小时 :02 生成一次"}
        </div>
      )}

      {brief?.mainline && (
        <div
          style={{
            fontSize: dense ? 12.5 : 13,
            lineHeight: 1.7,
            padding: "8px 10px",
            borderRadius: 6,
            background: colors.bg,
            border: `1px solid ${colors.border}`,
            marginBottom: 10,
            fontWeight: 500,
          }}
        >
          <span style={{ fontWeight: 700, marginRight: 6, color: colors.accent }}>主线</span>
          {brief.mainline}
        </div>
      )}

      {/* —— 综述（默认展示前两段，可展开全文）—— */}
      {paragraphs.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 12, color: colors.muted, marginBottom: 4 }}>
            今日综述
            <span style={{ marginLeft: 6, opacity: 0.8 }}>
              （{paragraphs.length} 段 · {brief?.summary?.length ?? 0} 字，每轮重写）
            </span>
          </div>
          <div style={{ fontSize: 12.5, lineHeight: 1.85 }}>
            {(summaryOpen ? paragraphs : paragraphs.slice(0, 2)).map((p, i) => (
              <p key={i} style={{ margin: "0 0 8px" }}>{p}</p>
            ))}
          </div>
          {paragraphs.length > 2 && (
            <button
              onClick={() => setSummaryOpen((v) => !v)}
              style={{
                border: 0, background: "transparent", color: colors.accent,
                cursor: "pointer", fontSize: 11.5, padding: 0,
              }}
            >
              {summaryOpen ? "收起 ▴" : `展开全文（还有 ${paragraphs.length - 2} 段）▾`}
            </button>
          )}
        </div>
      )}

      {/* —— 今日主题（各轮合并去重）—— */}
      {items.length > 0 && (
        <>
          <div style={{ fontSize: 12, color: colors.muted, margin: "2px 0 6px" }}>
            今日主题 · {items.length} 条（按首次出现倒序）
          </div>
          <div
            style={{
              display: columns === 2 ? "grid" : "flex",
              gridTemplateColumns: columns === 2 ? "1fr 1fr" : undefined,
              flexDirection: columns === 2 ? undefined : "column",
              gap: dense ? 7 : 9,
            }}
          >
            {shownItems.map((it, i) => {
              const up = it.direction === "bullish";
              const tone = up
                ? colors.up
                : it.direction === "bearish"
                  ? colors.down
                  : colors.muted;
              return (
                <div
                  key={i}
                  style={{ display: "flex", gap: 9, alignItems: "flex-start", minWidth: 0 }}
                >
                  <Badge
                    text={`${up ? "多" : it.direction === "bearish" ? "空" : "中"} ${it.strength}/5`}
                    color={tone}
                    soft
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{
                        fontSize: dense ? 12 : 12.5,
                        fontWeight: 600,
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                        flexWrap: "wrap",
                      }}
                    >
                      <span>{it.theme}</span>
                      <SourceTag item={it} />
                      {it.first_seen && (
                        <span style={{ fontSize: 10.5, color: colors.muted, fontWeight: 400 }}>
                          {it.first_seen.slice(11, 16)}
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 2 }}>
                      {it.sectors?.length ? <>板块 {it.sectors.join("/")}　</> : null}
                      {it.tickers?.length ? <>个股 {it.tickers.join("/")}　</> : null}
                      {it.horizon}
                    </div>
                    <div
                      style={{
                        fontSize: 11.5,
                        color: colors.text,
                        opacity: 0.85,
                        marginTop: 2,
                      }}
                    >
                      {it.why}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
          {items.length > ITEMS_PREVIEW && (
            <button
              onClick={() => setShowAllItems((v) => !v)}
              style={{
                marginTop: 8, border: 0, background: "transparent", color: colors.accent,
                cursor: "pointer", fontSize: 11.5, padding: 0,
              }}
            >
              {showAllItems
                ? "收起 ▴"
                : `显示全部 ${items.length} 条（还有 ${items.length - ITEMS_PREVIEW} 条）▾`}
            </button>
          )}
        </>
      )}

      {!!brief?.inputs?.length && (
        <InputsPanel
          inputs={brief.inputs}
          cited={new Set(items.flatMap((it) => it.source_refs || []))}
        />
      )}

      <div style={{ marginTop: 9, fontSize: 11, color: colors.muted, lineHeight: 1.6 }}>
        输入 = 财联社一手快讯（时效）+ DeepSeek 网页搜索（覆盖广度），模型只做筛选与判断；
        每小时 :02 增量生成，本卡片展示今日累计（跨轮去重），因此 DeepSeek 的条目
        不会因为「上一轮报过」而从页面上消失。
        {columns === 1 && (
          <>
            {" "}来源标签：灰色「财联社电报」= 一手快讯，蓝色「DeepSeek · 站点」= 网页搜索（可点开原文）。
          </>
        )}
        {" "}
        <a
          href="#news"
          style={{ color: colors.accent, textDecoration: "none", whiteSpace: "nowrap" }}
        >
          全部原始资讯 →
        </a>
      </div>
    </Card>
  );
}
