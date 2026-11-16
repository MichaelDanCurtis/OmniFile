import { Tabs } from "../../../components/PageHead";

/** The migrations area's three views, as tabs (the same links the console always had). */
export function MigrationTabs() {
  return (
    <Tabs
      label="Migrations views"
      items={[
        { to: "/migrations", label: "Console", end: true },
        { to: "/migrations/agents", label: "Agents" },
        { to: "/migrations/plans", label: "Plans" },
      ]}
    />
  );
}
