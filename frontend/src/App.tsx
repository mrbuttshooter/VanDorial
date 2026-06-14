import { HashRouter, Route, Routes, Link } from "react-router-dom";
import { Shell } from "./components/layout/Shell";
import { ToastProvider } from "./components/ui/Toast";
import { FleetScopeProvider } from "./fleet/scope";
import { Dashboard } from "./pages/Dashboard";
import { Campaigns } from "./pages/Campaigns";
import { Scenarios } from "./pages/Scenarios";
import { Connectors } from "./pages/Connectors";
import { Loops } from "./pages/Loops";
import { Performance } from "./pages/Performance";
import { History } from "./pages/History";
import { Config } from "./pages/Config";
import { Nodes } from "./pages/Nodes";
import { Groups } from "./pages/Groups";
import { EmptyState } from "./components/ui/Misc";
import { Button } from "./components/ui/Button";

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
              <Route path="nodes" element={<Nodes />} />
              <Route path="groups" element={<Groups />} />
              <Route path="campaigns" element={<Campaigns />} />
              <Route path="scenarios" element={<Scenarios />} />
              <Route path="connectors" element={<Connectors />} />
              <Route path="loops" element={<Loops />} />
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
          </Routes>
        </HashRouter>
      </FleetScopeProvider>
    </ToastProvider>
  );
}
