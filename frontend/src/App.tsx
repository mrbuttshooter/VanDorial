import { useCallback, useEffect, useState } from "react";
import { HashRouter, Route, Routes, Link } from "react-router-dom";
import { Shell } from "./components/layout/Shell";
import { ToastProvider } from "./components/ui/Toast";
import { FleetScopeProvider } from "./fleet/scope";
import { Dashboard } from "./pages/Dashboard";
import { Campaigns } from "./pages/Campaigns";
import { Scenarios } from "./pages/Scenarios";
import { Connectors } from "./pages/Connectors";
import { Fleet } from "./pages/Fleet";
import { Loops } from "./pages/Loops";
import { Performance } from "./pages/Performance";
import { History } from "./pages/History";
import { Config } from "./pages/Config";
import { Nodes } from "./pages/Nodes";
import { Groups } from "./pages/Groups";
import { Login } from "./pages/Login";
import { EmptyState, Spinner } from "./components/ui/Misc";
import { Button } from "./components/ui/Button";
import { api, ApiError, bootstrapApiKey, getApiKey, onAuthRequired } from "./lib/api";

type AuthState = "checking" | "authed" | "login";

/** Verify a session on boot: trust a stored token if /api/auth/me accepts it;
    otherwise fall back to the legacy console bootstrap ONCE (covers the
    zero-users migration window + headless boxes that still mint a key) and
    re-check. Anything left unresolved drops to the Login page. */
async function probeAuth(): Promise<boolean> {
  const tryMe = async () => {
    try {
      await api.me();
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return false;
      // Network/other error: don't bounce the operator to login over a blip.
      return getApiKey() != null;
    }
  };

  if (getApiKey() && (await tryMe())) return true;
  await bootstrapApiKey();
  return getApiKey() != null && (await tryMe());
}

function AuthGate() {
  const [state, setState] = useState<AuthState>("checking");

  const runProbe = useCallback(() => {
    setState("checking");
    probeAuth().then((ok) => setState(ok ? "authed" : "login"));
  }, []);

  useEffect(() => {
    runProbe();
    // An unrecoverable 401 anywhere in the app clears the token and fires this.
    return onAuthRequired(() => setState("login"));
  }, [runProbe]);

  if (state === "checking") {
    return (
      <div style={{ height: "100vh", display: "grid", placeItems: "center" }}>
        <Spinner />
      </div>
    );
  }

  if (state === "login") {
    return <Login onSuccess={() => setState("authed")} />;
  }

  return (
    <FleetScopeProvider>
      {/* HashRouter keeps deep links working when FastAPI serves the SPA from
          a static mount without per-route rewrites. */}
      <HashRouter>
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<Dashboard />} />
            {/* Fleet control plane (design §7) */}
            <Route path="fleet" element={<Fleet />} />
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
  );
}

export default function App() {
  return (
    <ToastProvider>
      <AuthGate />
    </ToastProvider>
  );
}
