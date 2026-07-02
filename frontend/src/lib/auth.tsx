import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "./api";
import type { MeResult } from "./types";

/** Current principal (from /api/auth/me), shared app-wide so pages can hide
    write controls for a read-only viewer. The backend enforces RBAC regardless
    — this is a UX affordance, not the security boundary. Defaults to
    full-access until /me resolves so controls never flicker for operators. */
interface AuthCtx {
  me: MeResult | null;
  canWrite: boolean;
  isAdmin: boolean;
  role: string;
}

const Ctx = createContext<AuthCtx>({
  me: null,
  canWrite: true,
  isAdmin: true,
  role: "operator",
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<MeResult | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .me()
      .then((m) => alive && setMe(m))
      .catch(() => {
        /* a 401 is handled by the AuthGate; ignore here */
      });
    return () => {
      alive = false;
    };
  }, []);

  const value: AuthCtx = {
    me,
    // Until /me resolves, assume writable so operator controls don't flash
    // disabled; a viewer's writes are still refused by the backend.
    canWrite: me ? me.can_write : true,
    isAdmin: me ? me.role === "admin" || me.role === "machine" : true,
    role: me?.role ?? "operator",
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  return useContext(Ctx);
}
