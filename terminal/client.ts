import { httpFetch } from "gloomberb/utils";

/**
 * Session client for the qsdash API.
 *
 * qsdash authenticates with a password and a TOTP code and returns a session
 * cookie. The plugin keeps that cookie in memory only: nothing about the
 * session, and never the password, is written to plugin config or disk, so a
 * restart asks for the TOTP code again, as the web dashboard does.
 *
 * The client is read-only by design. Every control action (mode, kill,
 * capital) needs a fresh re-authentication and a CSRF token on qsdash, and
 * stays in the web dashboard where that flow is built.
 */

export const SESSION_COOKIE = "qs_session";
const TIMEOUT_MS = 10_000;

export type Fetcher = (url: string, init?: RequestInit) => Promise<Response>;

export interface SessionState {
  baseUrl: string | null;
  username: string | null;
  connected: boolean;
  /** Why the last login or read failed, for the panes' empty state. */
  error: string | null;
}

export class QsApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "QsApiError";
  }
}

/**
 * The base URL with no trailing slash, or an error message. Plain HTTP is
 * refused for anything but the loopback host: the login sends a password.
 */
export function normalizeBaseUrl(raw: string): { url: string } | { error: string } {
  let parsed: URL;
  try {
    parsed = new URL(raw.trim());
  } catch {
    return { error: `"${raw}" is not a URL` };
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return { error: "the qsdash URL must be http or https" };
  }
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);
  if (parsed.protocol === "http:" && !loopback) {
    return { error: "plain http is only allowed for localhost; use https for a remote qsdash" };
  }
  return { url: `${parsed.origin}${parsed.pathname.replace(/\/+$/, "")}` };
}

/** The session cookie's value from a login response, or null. */
export function sessionCookieFrom(headers: Headers): string | null {
  const values = typeof headers.getSetCookie === "function"
    ? headers.getSetCookie()
    : (headers.get("set-cookie") ?? "").split(/,(?=\s*[A-Za-z0-9_-]+=)/);
  for (const raw of values) {
    const [pair] = raw.split(";");
    const eq = pair?.indexOf("=") ?? -1;
    if (pair && eq > 0 && pair.slice(0, eq).trim() === SESSION_COOKIE) {
      const value = pair.slice(eq + 1).trim();
      return value || null;
    }
  }
  return null;
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // not JSON: fall back to the status line
  }
  return response.statusText || `HTTP ${response.status}`;
}

export class QsClient {
  private cookie: string | null = null;
  private state: SessionState = { baseUrl: null, username: null, connected: false, error: null };
  private readonly listeners = new Set<() => void>();

  constructor(private readonly fetcher: Fetcher = httpFetch) {}

  getState(): SessionState {
    return this.state;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private setState(next: Partial<SessionState>): void {
    this.state = { ...this.state, ...next };
    for (const listener of this.listeners) listener();
  }

  async login(baseUrl: string, username: string, password: string, totp: string): Promise<void> {
    const base = normalizeBaseUrl(baseUrl);
    if ("error" in base) {
      this.setState({ connected: false, error: base.error });
      throw new QsApiError(0, base.error);
    }
    const code = totp.replace(/\s+/g, "");
    if (!/^\d{6}$/.test(code)) {
      this.setState({ error: "the TOTP code is six digits" });
      throw new QsApiError(0, "the TOTP code is six digits");
    }
    const response = await this.fetcher(`${base.url}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ username, password, totp: code }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!response.ok) {
      const detail = await errorDetail(response);
      this.cookie = null;
      this.setState({ baseUrl: base.url, username, connected: false, error: detail });
      throw new QsApiError(response.status, detail);
    }
    const cookie = sessionCookieFrom(response.headers);
    if (!cookie) {
      const detail = "qsdash accepted the login but sent no session cookie";
      this.setState({ connected: false, error: detail });
      throw new QsApiError(response.status, detail);
    }
    this.cookie = cookie;
    this.setState({ baseUrl: base.url, username, connected: true, error: null });
  }

  /** Forget the session locally. The server session expires on its own timer. */
  disconnect(): void {
    this.cookie = null;
    this.setState({ connected: false, error: null });
  }

  async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    const { baseUrl } = this.state;
    if (!this.cookie || !baseUrl) throw new QsApiError(401, "not connected to qsdash");
    const timeout = AbortSignal.timeout(TIMEOUT_MS);
    const response = await this.fetcher(`${baseUrl}/api${path}`, {
      headers: { Accept: "application/json", Cookie: `${SESSION_COOKIE}=${this.cookie}` },
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    });
    if (response.status === 401) {
      // expired or revoked: every pane drops to the connect prompt at once
      this.cookie = null;
      this.setState({ connected: false, error: "the qsdash session expired; connect again" });
      throw new QsApiError(401, "the qsdash session expired");
    }
    if (!response.ok) throw new QsApiError(response.status, await errorDetail(response));
    return (await response.json()) as T;
  }
}

/** The one client every pane shares, so one login serves them all. */
export const qsClient = new QsClient();
