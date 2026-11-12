import { ReactNode, useEffect, useState } from "react";
import { HubTabs } from "./Hub";

/**
 * 合并壳：把两个原独立页面收编为一个 Tab 下的子页签（v2.1 精简导航）。
 * 子页全部保持挂载（display 切换），切换时表单/图表状态不丢失；
 * 原页面组件一行未改，合并可随时无损拆开。
 */
export function MergedTabs({
  tabs,
  defaultKey,
  forceKey,
}: {
  tabs: { key: string; label: string; node: ReactNode }[];
  /** 初始子页签（来自地址栏 #hub/id/inner）；不传或匹配不上就用第一个。 */
  defaultKey?: string;
  /** 深链变化时强制切到该子页（例如 #btopt/0/workset-accept → backtest）。 */
  forceKey?: string;
}) {
  const [active, setActive] = useState(
    () => tabs.find((t) => t.key === (forceKey || defaultKey))?.key ?? tabs[0].key,
  );
  useEffect(() => {
    if (!forceKey) return;
    const hit = tabs.find((t) => t.key === forceKey)?.key;
    if (hit) setActive(hit);
  }, [forceKey]);
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
