import { ReactNode, useState } from "react";
import { HubTabs } from "./Hub";

/**
 * 合并壳：把两个原独立页面收编为一个 Tab 下的子页签（v2.1 精简导航）。
 * 子页全部保持挂载（display 切换），切换时表单/图表状态不丢失；
 * 原页面组件一行未改，合并可随时无损拆开。
 */
export function MergedTabs({
  tabs,
}: {
  tabs: { key: string; label: string; node: ReactNode }[];
}) {
  const [active, setActive] = useState(tabs[0].key);
  return (
    <div>
      <HubTabs tabs={tabs} active={active} onChange={setActive} />
      {tabs.map((t) => (
        <div key={t.key} style={{ display: t.key === active ? "block" : "none" }}>
          {t.node}
        </div>
      ))}
    </div>
  );
}
