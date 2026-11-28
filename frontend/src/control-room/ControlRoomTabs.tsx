import { useState } from "react";
import type { ReactNode } from "react";
import { Tabs } from "../components/ui";

/**
 * One control-room view with two tabs (integration of Step Q + Step AF).
 * Q's registry (overview + approval-gated run/agent controls) and AF's live
 * dashboard (charts, alert config, deliveries) are separate pages; they
 * share the "control-room" nav entry and the same scope picker state lives
 * inside each page. Both pages are passed in as elements so the caller's
 * lazy loading is preserved.
 */
export function ControlRoomTabs({ registry, live }: { registry: ReactNode; live: ReactNode }) {
  const [tab, setTab] = useState("registry");
  return (
    <div>
      <Tabs
        className="mb-4"
        value={tab}
        onValueChange={setTab}
        items={[
          { value: "registry", label: "Registry" },
          { value: "live", label: "Live dashboard" },
        ]}
      />
      {tab === "registry" ? registry : live}
    </div>
  );
}
