import { createContext, useCallback, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, errorText, get, post, setReauthHandler, setUnauthorizedHandler } from "./api";
import { Button, Modal } from "./components/ui";

export interface Me {
  username: string;
  totp_enabled: boolean;
  csrf_token: string;
  last_login_at: string | null;
}

interface AuthCtx {
  me: Me | null;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

export function useAuth(): AuthCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside provider");
  return v;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);
  const [reauthOpen, setReauthOpen] = useState(false);
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  const refresh = useCallback(async () => {
    try {
      setMe(await get<Me>("/api/auth/me"));
    } catch {
      setMe(null);
    } finally {
      setChecked(true);
    }
  }, []);

  useEffect(() => {
    refresh();
    setUnauthorizedHandler(() => setMe(null));
    setReauthHandler(() => new Promise<boolean>((resolve) => {
      resolver.current = resolve;
      setReauthOpen(true);
    }));
    return () => {
      setReauthHandler(null);
      setUnauthorizedHandler(null);
    };
  }, [refresh]);

  const logout = useCallback(async () => {
    try {
      await post("/api/auth/logout");
    } finally {
      setMe(null);
    }
  }, []);

  const finish = (ok: boolean) => {
    setReauthOpen(false);
    resolver.current?.(ok);
    resolver.current = null;
  };

  if (!checked) return <div className="boot"><span className="spin" /></div>;
  return (
    <Ctx.Provider value={{ me, refresh, logout }}>
      {me ? children : <Login onDone={refresh} />}
      <ReauthDialog open={reauthOpen} totp={!!me?.totp_enabled} onDone={finish} />
    </Ctx.Provider>
  );
}

function Login({ onDone }: { onDone: () => Promise<void> }) {
  const [username, setU] = useState("");
  const [password, setP] = useState("");
  const [totp, setT] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const r = await post<{ ok?: boolean; totp_required?: boolean }>("/api/auth/login", { username, password, totp: needTotp ? totp : null });
      if (r.totp_required) {
        setNeedTotp(true);
      } else {
        await onDone();
      }
    } catch (e2) {
      setErr(e2 instanceof ApiError && e2.status === 429 ? "Too many attempts — wait a few minutes." : errorText(e2));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <div className="brand-lg"><img src="/icons/icon-192.png" alt="" width={44} height={44} /><div><b>KESTREL</b><span>BTC Perp Desk</span></div></div>
        {!needTotp ? (
          <>
            <label>Username<input autoComplete="username" value={username} onChange={(e) => setU(e.target.value)} autoFocus required /></label>
            <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setP(e.target.value)} required /></label>
          </>
        ) : (
          <label>Authentication code
            <input inputMode="numeric" autoComplete="one-time-code" value={totp} onChange={(e) => setT(e.target.value)} autoFocus
              placeholder="123456 or recovery code" required />
          </label>
        )}
        {err && <div className="alert a-red">{err}</div>}
        <Button type="submit" kind="primary" busy={busy}>{needTotp ? "Verify" : "Sign in"}</Button>
        <p className="fine">Trading BTC perpetual futures involves substantial risk. Leverage can result in rapid losses.
          Past performance does not guarantee future results.</p>
      </form>
    </div>
  );
}

function ReauthDialog({ open, totp, onDone }: { open: boolean; totp: boolean; onDone: (ok: boolean) => void }) {
  const [password, setP] = useState("");
  const [code, setC] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) { setP(""); setC(""); setErr(null); }
  }, [open]);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await post("/api/auth/reauth", { password, totp: totp ? code : null });
      onDone(true);
    } catch (e2) {
      setErr(errorText(e2));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={open} onClose={() => onDone(false)} title="Confirm it's you">
      <form className="stack" onSubmit={submit}>
        <p className="muted">This action changes live-trading or security settings. Re-enter your password{totp ? " and authentication code" : ""}.</p>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setP(e.target.value)} autoFocus required /></label>
        {totp && <label>Authentication code<input inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setC(e.target.value)} required /></label>}
        {err && <div className="alert a-red">{err}</div>}
        <div className="row end">
          <Button kind="ghost" onClick={() => onDone(false)}>Cancel</Button>
          <Button type="submit" kind="primary" busy={busy}>Confirm</Button>
        </div>
      </form>
    </Modal>
  );
}
