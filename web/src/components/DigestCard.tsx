/**
 * 当日全景综述卡片（brief_digest，每天 21:45 生成一次，deepseek-v4-pro）。
 *
 * 与「热点简报」的分工 —— 两个都是判断，但层次不同：
 *   简报 = 每轮增量（这几小时有什么新的），一天 8 次，只看自己那个窗口
 *   本卡 = 全天全景（一天在交易什么、线索怎么互相印证/矛盾、明天盯什么）
 *
 * 老刘的用法是早上打开首页先看这个，所以「今天还没生成」时要显示**上一份**
 * （昨天收盘后写的），而不是空白 —— 所以接口带 before=今天。
 */
import { useEffect, useState } from "react";
import { Badge, Card } from "./ui";
import { api, BriefDigest } from "../api/client";
import { useTheme } from "../theme";
import { highlight } from "./highlight";

const PREVIEW_CHARS = 420;

export default function DigestCard() {
  const { colors } = useTheme();
  const [d, setD] = useState<BriefDigest | null>(null);
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState("");
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;

  useEffect(() => {
    api
      .briefDigestLatest(todayStr)
      .then((r) => setD(r && r.day ? r : null))
      .catch((e) => setErr(String(e?.message || e)));
  }, [todayStr]);

  if (err) return null;
  if (!d) return null;

  const body = d.summary || "";
  const isToday = d.day === todayStr;
  const stats = d.stats || {};
  const shown = open ? body : body.slice(0, PREVIEW_CHARS);
  const rest = body.length - shown.length;

  return (
    <Card
      colors={colors}
      title={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          当日全景
          <Badge text={d.day} color={isToday ? colors.accent : colors.muted} soft />
          {isToday ? (
            <span style={{ fontSize: 11, color: colors.muted }}>今日已收盘后生成</span>
          ) : (
            <span style={{ fontSize: 11, color: colors.muted }}>最近一份</span>
          )}
        </span>
      }
      extra={
        <span style={{ fontSize: 11, color: colors.muted }}>
          {d.chars || body.length} 字 · {d.model}
        </span>
      }
      style={{ marginBottom: 14 }}
    >
      <div
        style={{
          fontSize: 13,
          lineHeight: 1.85,
          color: colors.text,
          whiteSpace: "pre-wrap",
        }}
      >
        {highlight(shown, colors)}
        {!open && rest > 0 && (
          <span style={{ color: colors.muted }}>…（还有 {rest} 字）</span>
        )}
      </div>

      {body.length > PREVIEW_CHARS && (
        <div
          onClick={() => setOpen(!open)}
          style={{
            marginTop: 8,
            fontSize: 12,
            color: colors.accent,
            cursor: "pointer",
            userSelect: "none",
          }}
        >
          {open ? "▴ 收起" : "▾ 展开全文"}
        </div>
      )}

      <div
        style={{
          marginTop: 10,
          paddingTop: 8,
          borderTop: `1px solid ${colors.border}`,
          fontSize: 11,
          color: colors.muted,
          display: "flex",
          gap: 14,
          flexWrap: "wrap",
        }}
      >
        <span>
          素材：{d.n_items} 条要点 / {d.n_news} 条原始资讯
        </span>
        {!!stats.sector_stat && stats.sector_stat !== "无" && (
          <span>板块：{stats.sector_stat}</span>
        )}
        {!!stats.hot_stocks && stats.hot_stocks !== "无" && (
          <span>个股：{stats.hot_stocks}</span>
        )}
        {!!stats.src_stat && <span>来源：{stats.src_stat}</span>}
      </div>
    </Card>
  );
}
