import { describe, expect, test } from "bun:test";

import { normalizeBaseUrl, QsApiError, QsClient, sessionCookieFrom, type Fetcher } from "./client";

/** A qsdash stand-in: records every request and answers from a script. */
function fakeServer(answer: (url: string, init: RequestInit) => Response) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetcher: Fetcher = async (url, init = {}) => {
    calls.push({ url, init });
    return answer(url, init);
  };
  return { calls, fetcher };
}

const json = (body: unknown, init: ResponseInit = {}) =>
  new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" }, ...init });

function loginOk(): Response {
  const headers = new Headers({ "Content-Type": "application/json" });
  headers.append("Set-Cookie", "qs_session=tok-123; HttpOnly; SameSite=Strict; Path=/");
  headers.append("Set-Cookie", "qs_csrf=csrf-9; SameSite=Strict; Path=/");
  return new Response(JSON.stringify({ ok: true }), { headers });
}

describe("normalizeBaseUrl", () => {
  test("strips the trailing slash and keeps a path prefix", () => {
    expect(normalizeBaseUrl("https://quant.example.com/")).toEqual({ url: "https://quant.example.com" });
    expect(normalizeBaseUrl("http://127.0.0.1:8000")).toEqual({ url: "http://127.0.0.1:8000" });
    expect(normalizeBaseUrl("https://host/qs/")).toEqual({ url: "https://host/qs" });
  });

  test("refuses plain http to anything but loopback: the login sends a password", () => {
    expect(normalizeBaseUrl("http://quant.example.com")).toHaveProperty("error");
    expect(normalizeBaseUrl("http://localhost:8000")).toEqual({ url: "http://localhost:8000" });
  });

  test("refuses a non-URL and other schemes", () => {
    expect(normalizeBaseUrl("not a url")).toHaveProperty("error");
    expect(normalizeBaseUrl("ftp://host")).toHaveProperty("error");
  });
});

describe("sessionCookieFrom", () => {
  test("picks the session cookie among several", () => {
    const headers = new Headers();
    headers.append("Set-Cookie", "qs_csrf=abc; Path=/");
    headers.append("Set-Cookie", "qs_session=the-token; HttpOnly; Path=/");
    expect(sessionCookieFrom(headers)).toBe("the-token");
  });

  test("returns null when there is none, or it is empty", () => {
    expect(sessionCookieFrom(new Headers())).toBeNull();
    const empty = new Headers();
    empty.append("Set-Cookie", "qs_session=; Path=/");
    expect(sessionCookieFrom(empty)).toBeNull();
  });
});

describe("QsClient", () => {
  test("logs in, then sends the session cookie on reads", async () => {
    const server = fakeServer((url) => (url.endsWith("/api/auth/login") ? loginOk() : json({ mode: "paper" })));
    const client = new QsClient(server.fetcher);
    await client.login("http://127.0.0.1:8000/", "op", "pw", " 123 456 ");

    const login = server.calls[0]!;
    expect(login.url).toBe("http://127.0.0.1:8000/api/auth/login");
    expect(JSON.parse(String(login.init.body))).toEqual({ username: "op", password: "pw", totp: "123456" });
    expect(client.getState()).toMatchObject({ connected: true, username: "op", error: null });

    expect(await client.get<{ mode: string }>("/overview")).toEqual({ mode: "paper" });
    const read = server.calls[1]!;
    expect(read.url).toBe("http://127.0.0.1:8000/api/overview");
    expect(new Headers(read.init.headers).get("Cookie")).toBe("qs_session=tok-123");
  });

  test("never sends a request for a malformed TOTP code", async () => {
    const server = fakeServer(() => loginOk());
    const client = new QsClient(server.fetcher);
    await expect(client.login("http://127.0.0.1:8000", "op", "pw", "12ab56")).rejects.toThrow("six digits");
    expect(server.calls).toHaveLength(0);
  });

  test("a refused login keeps the server's reason and stays disconnected", async () => {
    const server = fakeServer(() => json({ detail: "invalid credentials" }, { status: 401 }));
    const client = new QsClient(server.fetcher);
    await expect(client.login("http://127.0.0.1:8000", "op", "bad", "000000")).rejects.toBeInstanceOf(QsApiError);
    expect(client.getState()).toMatchObject({ connected: false, error: "invalid credentials" });
  });

  test("a login that returns no cookie is not a session", async () => {
    const server = fakeServer(() => json({ ok: true }));
    const client = new QsClient(server.fetcher);
    await expect(client.login("http://127.0.0.1:8000", "op", "pw", "123456")).rejects.toThrow("no session cookie");
    expect(client.getState().connected).toBe(false);
  });

  test("a 401 on a read drops the session and tells every subscriber", async () => {
    let expired = false;
    const server = fakeServer((url) => {
      if (url.endsWith("/login")) return loginOk();
      return expired ? json({ detail: "session expired" }, { status: 401 }) : json([]);
    });
    const client = new QsClient(server.fetcher);
    await client.login("http://127.0.0.1:8000", "op", "pw", "123456");
    let notified = 0;
    client.subscribe(() => { notified += 1; });

    expired = true;
    await expect(client.get("/positions")).rejects.toMatchObject({ status: 401 });
    expect(client.getState().connected).toBe(false);
    expect(notified).toBe(1);
    // and no further read goes out without a new login
    await expect(client.get("/positions")).rejects.toThrow("not connected");
    expect(server.calls).toHaveLength(2);
  });

  test("a server error surfaces its detail and keeps the session", async () => {
    const server = fakeServer((url) => (url.endsWith("/login") ? loginOk()
      : json({ detail: "decision not found" }, { status: 404 })));
    const client = new QsClient(server.fetcher);
    await client.login("http://127.0.0.1:8000", "op", "pw", "123456");
    await expect(client.get("/decisions/9")).rejects.toThrow("decision not found");
    expect(client.getState().connected).toBe(true);
  });

  test("disconnect forgets the cookie", async () => {
    const server = fakeServer(() => loginOk());
    const client = new QsClient(server.fetcher);
    await client.login("http://127.0.0.1:8000", "op", "pw", "123456");
    client.disconnect();
    await expect(client.get("/overview")).rejects.toThrow("not connected");
  });
});
