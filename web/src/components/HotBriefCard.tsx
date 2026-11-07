/**
 * 热点简报卡片（按交易时段生成，展示「本轮新增 + 今日累计」）。
 *
 * 时刻表（2026-11-07 起）：交易日 07/09/10/11/13/14/16/21 点各一轮，
 * 非交易日只跑 09/21 点 —— 见 core/hot_brief_scheduler.py。两次之间可能隔
 * 10 小时（21 点收工到次日 7 点），所以叫「本轮新增」而不是「本小时」。
 *
 * 三块：
 *   ① 本轮新增 —— 最近一轮真正新报的条目。老刘的预期是「看这一轮的新消息」，
 *      所以它必须在最上面；若本轮确实没有新材料，明确写「无新增」而不是留白。
 *   ② 今日综述   —— 350~1000 字，分政策/产业/海外/资金四段，每轮重写覆盖当日整体。
 *   ③ 今日主题   —— 各轮合并 + LLM 语义去重后的清单，带来源与首次出现时间。
 *
 * 关键词高亮只做视觉锚点：政策/产业/市场词用主色，数字用浅底，**不用红绿**——
 * 同一个数字在不同语境下多空相反，上色会误导。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Card } from "./ui";
import { api, HotBrief, HotBriefInput, HotBriefItem } from "../api/client";
import { ThemeColors, useTheme } from "../theme";

const ITEMS_PREVIEW = 8;

/** 综述高亮词表：政策与监管 / 产业 / 市场口径。 */
const HL_KEYWORDS = [
  "国常会", "政治局", "证监会", "央行", "发改委", "工信部", "商务部", "财政部",
  "降准", "降息", "再贷款", "逆回购", "MLF", "LPR", "专项债", "财政", "监管",
  "立案", "反倾销", "关税", "制裁", "回购", "增持", "减持", "中标",
  "半导体", "算力", "人工智能", "新能源", "光伏", "储能", "军工",
  "黄金", "原油", "煤炭", "化工", "医药", "地产", "汽车", "机器人", "信创",
  "成交额", "两融", "北向", "估值", "股息", "ETF", "涨停", "跌停", "换手",
];

const KW_RE = new RegExp(`(${HL_KEYWORDS.join("|")})`, "g");
const NUM_RE = /(\d[\d,]*(?:\.\d+)?(?:万亿|亿|万|个百分点|个基点|bp|%|美元|盎司|吨|家|项|倍|点)?)/g;

function highlight(text: string, colors: ThemeColors): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  text.split(KW_RE).forEach((chunk, i) => {
    if (!chunk) return;
    if (HL_KEYWORDS.includes(chunk)) {
      out.push(
        <span key={`k${i}`} style={{ color: colors.accent, fontWeight: 600 }}>{chunk}</span>,
      );
      return;
    }
    chunk.split(NUM_RE).forEach((p, j) => {
      if (!p) return;
      if (/^\d/.test(p)) {
        out.push(
          <span key={`n${i}-${j}`} style={{
            fontWeight: 600, background: `${colors.accent}16`,
            padding: "0 1px", borderRadius: 2,
          }}>{p}</span>,
        );
      } else {
        out.push(<span key={`t${i}-${j}`}>{p}</span>);
      }
    });
  });
  return out;
}

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

/** 一条主题（方向徽标 + 主题 + 来源 + 时间 + 板块/个股 + 影响路径）。 */
function ItemRow({ it, dense }: { it: HotBriefItem; dense: boolean }) {
  const { colors } = useTheme();
  const up = it.direction === "bullish";
  const tone = up ? colors.up : it.direction === "bearish" ? colors.down : colors.muted;
  return (
    <div style={{ display: "flex", gap: 9, alignItems: "flex-start", minWidth: 0 }}>
      <Badge
        text={`${up ? "多" : it.direction === "bearish" ? "空" : "中"} ${it.strength}/5`}
        color={tone}
        soft
      />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize: dense ? 12 : 12.5, fontWeight: 600,
          display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap",
        }}>
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
        <div style={{ fontSize: 11.5, color: colors.text, opacity: 0.85, marginTop: 2 }}>
          {it.why}
        </div>
      </div>
    </div>
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

/** 正文是 Markdown（DeepSeek 的 summary / fetch 的 content 都带 `##`、`**`），
 *  原样铺出来满屏符号。只剥标题井号和加粗星号，段落结构保留。 */
function cleanBody(s: string): string {
  return (s || "")
    .replace(/^\s*#{1,6}\s*/gm, "")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/**
 * 可展开的「本轮原始输入」。
 *
 * ● = 被模型引用（进了某条主题的 src），○ = 看过但丢弃。
 * 点任意一行展开正文快照 —— 老刘 2026-11-07 的要求是「原始抓下来的文章也要能看」，
 * 之前只给标题 + 外链，想看内容得跳走，等于没给。
 * 「抓全文」才走 `/v1/tools/fetch` 实时抓整页（联网 + 按次计费），不预取。
 */
function InputsPanel({ inputs, cited }: { inputs: HotBriefInput[]; cited: Set<string> }) {
  const { colors } = useTheme();
  const [open, setOpen] = useState(false);
  const [openRef, setOpenRef] = useState<string | null>(null);
  const [fulls, setFulls] = useState<
    Record<string, { loading?: boolean; content?: string; error?: string }>
  >({});
  const used = inputs.filter((x) => cited.has(x.ref)).length;

  const grabFull = (x: HotBriefInput) => {
    setFulls((m) => ({ ...m, [x.ref]: { loading: true } }));
    api
      .hotBriefInput(x.ref, { full: true })
      .then((d) =>
        setFulls((m) => ({
          ...m,
          [x.ref]: { content: d.content || "", error: d.error },
        })),
      )
      .catch((e) =>
        setFulls((m) => ({ ...m, [x.ref]: { error: String(e?.message || e) } })),
      );
  };

  return (
    <div style={{ marginTop: 10, borderTop: `1px solid ${colors.border}`, paddingTop: 8 }}>
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          border: 0, background: "transparent", color: colors.accent, cursor: "pointer",
          fontSize: 11.5, padding: 0,
        }}
      >
        {open ? "▾" : "▸"} 本轮原始输入 {inputs.length} 条（已采用 {used} 条，其余被丢弃；点标题看正文）
      </button>
      {open && (
        <div style={{ marginTop: 6, maxHeight: 420, overflowY: "auto" }}>
          {inputs.map((x) => {
            const on = cited.has(x.ref);
            const isOpen = openRef === x.ref;
            const f = fulls[x.ref];
            return (
              <div key={x.ref} style={{ borderBottom: `1px solid ${colors.border}` }}>
                <div
                  onClick={() => setOpenRef(isOpen ? null : x.ref)}
                  style={{
                    display: "flex", gap: 8, alignItems: "baseline", cursor: "pointer",
                    padding: "4px 0", fontSize: 11.5, opacity: on ? 1 : 0.55,
                  }}
                  title={x.title}
                >
                  <span style={{ color: on ? colors.accent : colors.muted, width: 8 }}>
                    {on ? "●" : "○"}
                  </span>
                  <span style={{ color: colors.muted, width: 26, flexShrink: 0 }}>{x.ref}</span>
                  <span style={{ color: colors.muted, width: 40, flexShrink: 0 }}>
                    {fmtInputTime(x)}
                  </span>
                  <span style={{ flex: 1, minWidth: 0, color: colors.text }}>{x.title}</span>
                  <span style={{ color: colors.muted, flexShrink: 0 }}>{x.site}</span>
                  <span style={{ color: colors.accent, flexShrink: 0 }}>{isOpen ? "▴" : "▾"}</span>
                </div>

                {isOpen && (
                  <div style={{ padding: "2px 0 10px 42px" }}>
                    <div style={{ fontSize: 10.5, color: colors.muted, marginBottom: 4 }}>
                      {x.kind} · {x.site}
                      {x.body ? ` · 正文 ${x.body.length} 字` : " · 无正文快照"}
                    </div>

                    {x.body ? (
                      <div style={{
                        fontSize: 11.5, lineHeight: 1.75, color: colors.text,
                        whiteSpace: "pre-wrap", maxHeight: 240, overflowY: "auto",
                        background: colors.bg, border: `1px solid ${colors.border}`,
                        borderRadius: 6, padding: "7px 9px",
                      }}>
                        {cleanBody(x.body)}
                      </div>
                    ) : (
                      <div style={{
                        fontSize: 11.5, color: colors.muted, padding: "6px 9px",
                        border: `1px dashed ${colors.border}`, borderRadius: 6,
                      }}>
                        这一轮没存正文快照（加正文之前生成的旧记录）。
                      </div>
                    )}

                    <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 6 }}>
                      {x.url ? (
                        <button
                          onClick={() => grabFull(x)}
                          disabled={!!f?.loading}
                          style={{
                            border: `1px solid ${colors.border}`, background: colors.card,
                            color: colors.muted, borderRadius: 4, padding: "1px 7px",
                            fontSize: 11, cursor: f?.loading ? "default" : "pointer",
                          }}
                        >
                          {f?.loading ? "抓取中…" : "抓整页正文"}
                        </button>
                      ) : null}
                      {x.url ? (
                        <a href={x.url} target="_blank" rel="noreferrer"
                           style={{ fontSize: 11, color: colors.accent, textDecoration: "none" }}>
                          打开原文 ↗
                        </a>
                      ) : null}
                    </div>

                    {f?.error && (
                      <div style={{ fontSize: 11, color: colors.down, marginTop: 5 }}>
                        抓取失败：{f.error}
                      </div>
                    )}
                    {f?.content && (
                      <div style={{
                        marginTop: 6, fontSize: 11.5, lineHeight: 1.75, color: colors.text,
                        whiteSpace: "pre-wrap", maxHeight: 320, overflowY: "auto",
                        border: `1px solid ${colors.border}`, borderRadius: 6, padding: "7px 9px",
                      }}>
                        <div style={{ fontSize: 10.5, color: colors.muted, marginBottom: 4 }}>
                          整页正文 · {f.content.length} 字（DeepSeek fetch 实时抓取）
                        </div>
                        {cleanBody(f.content)}
                      </div>
                    )}
                  </div>
                )}
              </div>
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
      .then((d) => setBrief(d && (d.n_items || d.summary) ? d : null))
      .catch(() => setBrief(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const items = brief?.items || [];
  const latest = brief?.latest_items || [];
  const paragraphs = useMemo(
    () => (brief?.summary || "").split(/\n+/).map((s) => s.trim()).filter(Boolean),
    [brief?.summary],
  );
  const shownItems = showAllItems ? items : items.slice(0, ITEMS_PREVIEW);
  const split = brief?.source_split || {};
  const splitText = ["快讯", "搜索", "混合", "未标注"]
    .filter((k) => (split[k] || 0) > 0)
    .map((k) => `${k} ${split[k]}`)
    .join(" · ");

  const meta = brief ? (
    <span style={{ fontSize: 11.5, color: colors.muted, fontWeight: 400 }}>
      {brief.date || ""} · 共 {brief.n_briefs ?? 1} 轮 · 今日 {brief.n_items} 条
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
          {loading ? "加载中…" : "尚无简报 —— 交易日 7/9/10/11/13/14/16/21 点各生成一轮"}
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
          {highlight(brief.mainline, colors)}
        </div>
      )}

      {/* —— ① 本轮新增 —— */}
      {brief && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 12, color: colors.muted, marginBottom: 5 }}>
            本轮新增
            <span style={{ marginLeft: 6 }}>
              （{brief.latest_at || "—"} 更新 · {brief.n_latest ?? latest.length} 条）
            </span>
          </div>
          {latest.length > 0 ? (
            <div style={{ display: "flex", flexDirection: "column", gap: dense ? 7 : 9 }}>
              {latest.map((it, i) => <ItemRow key={i} it={it} dense={dense} />)}
            </div>
          ) : (
            <div style={{
              fontSize: 12, color: colors.muted, padding: "6px 10px",
              border: `1px dashed ${colors.border}`, borderRadius: 6,
            }}>
              本轮无新增主题 —— 快讯与搜索都没有新材料，下方综述与今日主题仍会更新。
            </div>
          )}
        </div>
      )}

      {/* —— ② 今日综述 —— */}
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
              <p key={i} style={{ margin: "0 0 8px" }}>{highlight(p, colors)}</p>
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

      {/* —— ③ 今日主题（已 LLM 去重）—— */}
      {items.length > 0 && (
        <>
          <div style={{ fontSize: 12, color: colors.muted, margin: "2px 0 6px" }}>
            今日主题 · {items.length} 条（已合并同一事件，按首次出现倒序）
          </div>
          <div
            style={{
              display: columns === 2 ? "grid" : "flex",
              gridTemplateColumns: columns === 2 ? "1fr 1fr" : undefined,
              flexDirection: columns === 2 ? undefined : "column",
              gap: dense ? 7 : 9,
            }}
          >
            {shownItems.map((it, i) => <ItemRow key={i} it={it} dense={dense} />)}
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

      {!!brief && (brief.summary || latest.length > 0) && (
        <details style={{ marginTop: 10, borderTop: `1px solid ${colors.border}`, paddingTop: 8 }}>
          <summary style={{ cursor: "pointer", fontSize: 11.5, color: colors.accent }}>
            本轮模型原始输出（JSON）— 主线 / 综述 / 条目全部字段
          </summary>
          <pre style={{
            marginTop: 6, maxHeight: 340, overflow: "auto", fontSize: 10.5,
            lineHeight: 1.55, background: colors.bg, border: `1px solid ${colors.border}`,
            borderRadius: 6, padding: 8, whiteSpace: "pre-wrap", wordBreak: "break-all",
            color: colors.text,
          }}>
            {JSON.stringify(
              { model: brief.model || "", mainline: brief.mainline || "", summary: brief.summary || "", items: latest },
              null,
              2,
            )}
          </pre>
        </details>
      )}

      {!!brief?.inputs?.length && (
        <InputsPanel
          inputs={brief.inputs}
          cited={new Set([...items, ...latest].flatMap((it) => it.source_refs || []))}
        />
      )}

      <div style={{ marginTop: 9, fontSize: 11, color: colors.muted, lineHeight: 1.6 }}>
        输入 = 财联社一手快讯（时效）+ DeepSeek 网页搜索（覆盖广度），模型只做筛选与判断；
        交易日 7/9/10/11/13/14/16/21 点各生成一轮（非交易日 9/21 点），
        每轮的取数窗口是「距上一个时段」——早 7 点那轮会把隔夜消息一起带上。
        「本轮新增」是这一轮的新条目，「今日主题」是全天合并去重后的全貌。
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
