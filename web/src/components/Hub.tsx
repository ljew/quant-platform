import { ReactNode } from "react";
import { useTheme } from "../theme";

/** 二级 Tab 容器：枢纽页内部切换（回测/寻优/因子 等）。 */
export function HubTabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { key: string; label: string }[];
  active: string;
  onChange: (key: string) => void;
}) {
  const { colors } = useTheme();
  return (
    <div
      style={{
        display: "flex",
        gap: 4,
        borderBottom: `1px solid ${colors.border}`,
        marginBottom: 16,
      }}
    >
      {tabs.map((t) => {
        const on = t.key === active;
        return (
          <button
            key={t.key}
            onClick={() => onChange(t.key)}
            style={{
              border: 0,
              background: "transparent",
              color: on ? colors.accent : colors.muted,
              fontSize: 13.5,
              fontWeight: on ? 600 : 400,
              padding: "8px 16px",
              cursor: "pointer",
              borderBottom: `2px solid ${on ? colors.accent : "transparent"}`,
              marginBottom: -1,
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/** 枢纽页：一级入口 + 二级 Tab，子页作为内容嵌入。 */
export function Hub({
  tabs,
  active,
  onChange,
  children,
}: {
  tabs: { key: string; label: string }[];
  active: string;
  onChange: (key: string) => void;
  children: ReactNode;
}) {
  return (
    <div>
      <HubTabs tabs={tabs} active={active} onChange={onChange} />
      {children}
    </div>
  );
}
