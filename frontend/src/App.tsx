import { HashRouter, Route, Routes, Link } from "react-router-dom";
import { Shell } from "./components/layout/Shell";
import { ToastProvider } from "./components/ui/Toast";
import { FleetScopeProvider } from "./fleet/scope";
import { Dashboard } from "./pages/Dashboard";
import { Campaigns } from "./pages/Campaigns";
import { Scenarios } from "./pages/Scenarios";
import { Connectors } from "./pages/Connectors";
import { Loops } from "./pages/Loops";
import { Console } from "./pages/Console";
import { Performance } from "./pages/Performance";
import { History } from "./pages/History";
import { Config } from "./pages/Config";
import { FleetOverview } from "./pages/FleetOverview";
import { Nodes } from "./pages/Nodes";
import { Groups } from "./pages/Groups";
import { EmptyState } from "./components/ui/Misc";
import { Button } from "./components/ui/Button";
import { NextShell } from "./next/NextShell";
import { Overview as NextOverview } from "./next/pages/Overview";
import { Servers as NextServers } from "./next/pages/Servers";
import { Routes as NextRoutes } from "./next/pages/Routes";
import { Activity as NextActivity } from "./next/pages/Activity";
import { Testing as NextTesting } from "./next/pages/Testing";
import { Settings as NextSettings } from "./next/pages/Settings";

export default function App() {
  return (
    <ToastProvider>
      <FleetScopeProvider>
        {/* HashRouter keeps deep links working when FastAPI serves the SPA from
            a static mount without per-route rewrites. */}
        <HashRouter>
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<Dashboard />} />
              {/* Fleet control plane (design §7) */}
              <Route path="fleet" element={<FleetOverview />} />
              <Route path="nodes" element={<Nodes />} />
              <Route path="groups" element={<Groups />} />
              <Route path="campaigns" element={<Campaigns />} />
              <Route path="scenarios" element={<Scenarios />} />
              <Route path="connectors" element={<Connectors />} />
              <Route path="loops" element={<Loops />} />
              <Route path="console" element={<Console />} />
              <Route path="performance" element={<Performance />} />
              <Route path="history" element={<History />} />
              <Route path="config" element={<Config />} />
              <Route
                path="*"
                element={
                  <EmptyState
                    mark="404"
                    title="Signal lost"
                    hint="That route doesn't exist."
                    action={
                      <Link to="/">
                        <Button variant="primary" size="sm">
                          Back to dashboard
                        </Button>
                      </Link>
                    }
                  />
                }
              />
            </Route>

            {/* NEXT (v3) loop-first console — parallel UI at /next; classic
                console above is untouched. Preview on localhost. */}
            <Route path="/next" element={<NextShell />}>
              <Route index element={<NextOverview />} />
              <Route path="servers" element={<NextServers />} />
              <Route path="routes" element={<NextRoutes />} />
              <Route path="activity" element={<NextActivity />} />
              <Route path="testing" element={<NextTesting />} />
              <Route path="settings" element={<NextSettings />} />
            </Route>
          </Routes>
        </HashRouter>
      </FleetScopeProvider>
    </ToastProvider>
  );
}
