import { useEffect, useMemo, useRef, useState } from "react";
import { useTheme } from "../theme";
import MdView, { parseMarkdown } from "./MdView";

/**
 * 全屏文档浮层。
 *
 * 为什么不单独开一个路由页：文档是「看数据时顺手查」的东西，
 * 跳走再跳回来会丢掉看板上的滚动位置和图表缩放状态。浮层关掉即回到原处。
 *
 * 目录取二级标题（##），点击后滚到对应位置 —— 用容器 scrollTop 而不是
 * scrollIntoView，后者会连带把背后的页面一起滚走。
 */
export default function DocDrawer({
  open, onClose, title, subtitle, markdown,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  markdown: string;
}) {
  const { colors } = useTheme();
  const bodyRef = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState("");

  const blocks = useMemo(() => {
    const all = parseMarkdown(markdown);
    // 浮层头部已经显示标题，正文里重复的 H1 去掉，避免同一屏出现两次同名大标题
    return all[0]?.t === "h" && all[0].level === 1 ? all.slice(1) : all;
  }, [markdown]);
  const toc = useMemo(
    () => blocks.flatMap((b) => (b.t === "h" && b.level === 2 ? [{ id: b.id, text: b.text }] : [])),
    [blocks],
  );

  // ESC 关闭 + 锁背景滚动
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  const jump = (id: string) => {
    const box = bodyRef.current;
    const el = box?.querySelector<HTMLElement>(`#${id}`);
    if (!box || !el) return;
    box.scrollTo({ top: el.offsetTop - 6, behavior: "smooth" });
    setActive(id);
  };

  if (!open) return null;

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 1000,
        background: "rgba(6,10,18,0.55)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: "3vh 2vw",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(1120px, 96vw)", height: "94vh",
          background: colors.card, borderRadius: 12,
          border: `1px solid ${colors.border}`,
          boxShadow: "0 24px 60px rgba(0,0,0,0.35)",
          display: "flex", flexDirection: "column", overflow: "hidden",
        }}
      >
        {/* 头部 */}
        <div style={{
          display: "flex", alignItems: "center", gap: 12,
          padding: "13px 18px", borderBottom: `1px solid ${colors.border}`,
          flexShrink: 0,
        }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 14.5, fontWeight: 700, color: colors.text }}>{title}</div>
            {subtitle && (
              <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 2 }}>{subtitle}</div>
            )}
          </div>
          <button
            onClick={onClose}
            title="关闭（Esc）"
            style={{
              marginLeft: "auto", flexShrink: 0,
              background: "transparent", border: `1px solid ${colors.border}`,
              color: colors.muted, borderRadius: 6, width: 30, height: 28,
              cursor: "pointer", fontSize: 15, lineHeight: 1,
            }}
          >×</button>
        </div>

        <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
          {/* 目录 */}
          {toc.length > 2 && (
            <div style={{
              width: 196, flexShrink: 0, overflowY: "auto",
              borderRight: `1px solid ${colors.border}`,
              padding: "12px 8px 20px",
            }}>
              <div style={{
                fontSize: 10.5, color: colors.muted, letterSpacing: 0.4,
                padding: "0 8px 8px", textTransform: "uppercase",
              }}>目录</div>
              {toc.map((x) => {
                const on = active === x.id;
                return (
                  <div
                    key={x.id}
                    onClick={() => jump(x.id)}
                    style={{
                      fontSize: 12, lineHeight: 1.45, cursor: "pointer",
                      padding: "6px 8px", borderRadius: 5,
                      color: on ? colors.accent : colors.muted,
                      background: on ? colors.tableStripe : "transparent",
                      fontWeight: on ? 600 : 400,
                    }}
                  >{x.text}</div>
                );
              })}
            </div>
          )}

          {/* 正文 */}
          <div
            ref={bodyRef}
            style={{
              flex: 1, overflowY: "auto", padding: "16px 26px 40px",
              position: "relative",
            }}
          >
            <MdView blocks={blocks} />
          </div>
        </div>
      </div>
    </div>
  );
}
