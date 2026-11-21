import LegacyDashboard from "../LegacyDashboard";

/** The research harness runs separately. Point its API URL at that server to use the old dashboard. */
export default function LegacyResearchPage() {
  return <LegacyDashboard />;
}
