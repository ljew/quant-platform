/**
 * 热点简报卡片（每小时 :02 自动生成）。
 *
 * 首页「今日观点」与「因子实验室」共用，抽成组件是因为来源标注、跨轮去重
 * 这些口径必须一致 —— 两处各写一份 JSX 迟早会漂移。
 *
 * 每条主题右侧的来源标签：
 *   灰色「财联社电报」= 平台自采一手快讯
 *   蓝色「DeepSeek · 站点」= DeepSeek 网页搜索，可点开原文
 */
import { useCallback, useEffect, useState } from "react";
import { Badge, Card } from "./ui";
import { api, HotBrief, HotBriefItem } from "../api/client";
import { useTheme } from "../theme";

/** 单条来源标签（可点击）。抽出来是为了在窄列里也不挤。 */
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

  const load = useCallback(() => {
    setLoading(true);
    api
      .hotBriefLatest()
      // 后端无数据时返回 {}，不是 null
      .then((d) => setBrief(d && d.id ? d : null))
      .catch(() => setBrief(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const items = brief?.items || [];
  const meta = brief ? (
    <span style={{ fontSize: 11.5, color: colors.muted, fontWeight: 400 }}>
      {brief.created_at?.slice(5, 16).replace("T", " ")}
      {" · "}输入 {brief.n_input} 条
      {brief.n_search ? `（快讯 ${brief.n_flash ?? 0} + 搜索 ${brief.n_search}）` : ""}
      {" → "}保留 {brief.n_items} 条
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
            fontSize: dense ? 12 : 12.5,
            lineHeight: 1.7,
            padding: "8px 10px",
            borderRadius: 6,
            background: colors.bg,
            border: `1px solid ${colors.border}`,
            marginBottom: 10,
          }}
        >
          <span style={{ fontWeight: 600, marginRight: 6 }}>主线</span>
          {brief.mainline}
        </div>
      )}

      {items.length > 0 && (
        <div
          style={{
            display: columns === 2 ? "grid" : "flex",
            gridTemplateColumns: columns === 2 ? "1fr 1fr" : undefined,
            flexDirection: columns === 2 ? undefined : "column",
            gap: dense ? 7 : 9,
          }}
        >
          {items.map((it, i) => {
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
      )}

      <div style={{ marginTop: 9, fontSize: 11, color: colors.muted, lineHeight: 1.6 }}>
        输入 = 财联社一手快讯（时效）+ DeepSeek 网页搜索（覆盖广度），模型只做筛选与判断；
        已与上一条简报做过跨轮去重，本轮只报新增变化。
        {columns === 1 && (
          <>
            {" "}来源标签：灰色「财联社电报」= 一手快讯，蓝色「DeepSeek · 站点」= 网页搜索（可点开原文）。
          </>
        )}
      </div>
    </Card>
  );
}
