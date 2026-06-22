import { useState, type FormEvent } from "react";
import s from "./login.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Field, Spinner } from "@/components/ui/Misc";
import { IconWave } from "@/components/icons";
import { api, ApiError } from "@/lib/api";

/** Maps the backend's auth failure codes to operator-friendly copy. */
function loginErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.status) {
      case 401:
        return "Invalid username or password.";
      case 429:
        return "Too many attempts — wait a moment and try again.";
      case 503:
        return "Login is not configured on this controller.";
      default:
        return err.message || "Login failed.";
    }
  }
  return err instanceof Error ? err.message : "Login failed.";
}

/** Full-screen auth gate. On success it stores the session token (= API key)
    and calls onSuccess so the root can mount the app. */
export function Login({ onSuccess }: { onSuccess: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      await api.login(username.trim(), password);
      onSuccess();
    } catch (err) {
      setError(loginErrorMessage(err));
      setBusy(false);
    }
  };

  return (
    <div className={s.screen}>
      <form className={s.card} onSubmit={submit}>
        <Panel
          title={
            <>
              <IconWave width={16} height={16} /> GenCall SMC
            </>
          }
        >
          <div className={s.intro}>Sign in to the SMC Console.</div>

          <div className={s.fields}>
            <Field label="Username">
              <input
                type="text"
                autoComplete="username"
                autoFocus
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                disabled={busy}
              />
            </Field>
            <Field label="Password">
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={busy}
              />
            </Field>
          </div>

          {error && (
            <div className={s.error} role="alert">
              {error}
            </div>
          )}

          <Button
            type="submit"
            variant="primary"
            className={s.submit}
            disabled={busy || !username.trim() || !password}
          >
            {busy ? <Spinner /> : "Sign in"}
          </Button>
        </Panel>
      </form>
    </div>
  );
}
