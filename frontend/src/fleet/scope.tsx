/* ============================================================================
   Fleet scope context — the global "what am I looking at?" selector (design §7).

   The topbar exposes a Fleet ▸ Group ▸ Node selector; pages read the active
   scope to decide whether to show aggregate, per-group, or per-node telemetry,
   and node-scoped console pages route their calls through the controller proxy.

   Scope is intentionally lightweight client state (no persistence beyond the
   session) and lives here so the Shell, Sidebar topbar, and fleet pages share
   one source of truth without prop-drilling.
   ============================================================================ */
import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type ScopeKind = "fleet" | "group" | "node";

/** The currently selected vantage point over the cluster. */
export interface FleetScope {
  kind: ScopeKind;
  /** Group id when kind === "group". */
  groupId: number | null;
  /** Node id when kind === "node" (the controller-proxy target). */
  nodeId: number | null;
}

interface ScopeCtx {
  scope: FleetScope;
  /** Reset to the whole-fleet vantage. */
  selectFleet: () => void;
  selectGroup: (groupId: number) => void;
  selectNode: (nodeId: number) => void;
}

const DEFAULT_SCOPE: FleetScope = { kind: "fleet", groupId: null, nodeId: null };

const Ctx = createContext<ScopeCtx | null>(null);

export function FleetScopeProvider({ children }: { children: ReactNode }) {
  const [scope, setScope] = useState<FleetScope>(DEFAULT_SCOPE);

  const value = useMemo<ScopeCtx>(
    () => ({
      scope,
      selectFleet: () => setScope(DEFAULT_SCOPE),
      selectGroup: (groupId: number) =>
        setScope({ kind: "group", groupId, nodeId: null }),
      selectNode: (nodeId: number) =>
        setScope({ kind: "node", groupId: null, nodeId }),
    }),
    [scope],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useFleetScope(): ScopeCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useFleetScope must be used within FleetScopeProvider");
  return ctx;
}
