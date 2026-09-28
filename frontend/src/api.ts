/** Fetch wrapper: same-origin cookies, CSRF header, structured errors, step-up re-auth hook. */

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : (detail as { message?: string })?.message || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
  get reauthRequired(): boolean {
    return this.status === 403 && !!(this.detail as { reauth_required?: boolean })?.reauth_required;
  }
  get errors(): string[] {
    const d = this.detail as { errors?: string[]; failing?: string[] };
    return d?.errors || d?.failing || [];
  }
}

function csrf(): string {
  const m = document.cookie.match(/(?:^|;\s*)kestrel_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

type ReauthHandler = () => Promise<boolean>;
let reauthHandler: ReauthHandler | null = null;
let unauthorizedHandler: (() => void) | null = null;
export function setReauthHandler(h: ReauthHandler | null) {
  reauthHandler = h;
}
export function setUnauthorizedHandler(h: (() => void) | null) {
  unauthorizedHandler = h;
}

async function raw<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrf();
  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  let data: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const detail = (data as { detail?: unknown })?.detail ?? data;
    if (res.status === 401 && !path.startsWith("/api/auth/")) unauthorizedHandler?.();
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

/** Performs the request; if the server asks for step-up auth, prompts once and retries. */
export async function api<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  try {
    return await raw<T>(method, path, body);
  } catch (e) {
    if (e instanceof ApiError && e.reauthRequired && reauthHandler) {
      const ok = await reauthHandler();
      if (ok) return raw<T>(method, path, body);
    }
    throw e;
  }
}

export const get = <T = unknown>(path: string) => api<T>("GET", path);
export const post = <T = unknown>(path: string, body?: unknown) => api<T>("POST", path, body ?? {});
export const put = <T = unknown>(path: string, body?: unknown) => api<T>("PUT", path, body ?? {});
export const del = <T = unknown>(path: string) => api<T>("DELETE", path);

export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const extra = e.errors.length ? ` — ${e.errors.join("; ")}` : "";
    return `${e.message}${extra}`;
  }
  return e instanceof Error ? e.message : String(e);
}
