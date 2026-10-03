import { NextRequest } from "next/server";
import { beforeAll, describe, expect, it } from "vitest";
import { COOKIE_NAME, issue } from "@/lib/auth/session";

const SECRET = "p".repeat(40);

beforeAll(() => {
  process.env.DASHBOARD_DATABASE_URL = "postgresql://x:y@db:5432/z";
  process.env.DASHBOARD_SESSION_SECRET = SECRET;
});

const nowS = () => Math.floor(Date.now() / 1000);
// A real server always has a Host header; the test request has to say it.
const request = (path: string, cookie?: string) =>
  new NextRequest(`http://127.0.0.1:4400${path}`, {
    headers: { host: "127.0.0.1:4400", ...(cookie ? { cookie: `${COOKIE_NAME}=${cookie}` } : {}) },
  });

async function call(path: string, cookie?: string) {
  const { proxy } = await import("@/proxy");
  return proxy(request(path, cookie));
}

describe("the proxy", () => {
  it("turns away a request sent under a name that is not ours, signed in or not", async () => {
    const { proxy } = await import("@/proxy");
    const good = issue(SECRET, nowS());
    for (const host of ["evil.example", "evil.example:4400", "127.0.0.1.evil.example", "localhost.evil.example:4400", ""]) {
      const request = new NextRequest("http://127.0.0.1:4400/api/live", { headers: { host, cookie: `${COOKIE_NAME}=${good}` } });
      expect(proxy(request).status, `Host: ${host}`).toBe(421);
    }
  });

  it("sends a visitor with no session to sign in, and answers an API call with 401", async () => {
    const page = await call("/");
    const api = await call("/api/live");

    expect(page.status).toBe(303);
    expect(page.headers.get("location")).toBe("/signin");
    expect(api.status).toBe(401);
  });

  it("lets the sign-in page, its form and the health check through without one", async () => {
    for (const path of ["/signin", "/api/signin", "/healthz"]) expect((await call(path)).status).toBe(200);
  });

  it("lets a signed-in visitor through, and refuses a forged or expired cookie", async () => {
    const good = issue(SECRET, nowS());
    const forged = issue("q".repeat(40), nowS());
    const old = issue(SECRET, nowS() - 3 * 3600);

    expect((await call("/", good)).status).toBe(200);
    expect((await call("/", forged)).status).toBe(303);
    expect((await call("/", old)).status).toBe(303);
  });

  it("puts a fresh nonce CSP that allows nothing inline and no other origin on every answer", async () => {
    const first = (await call("/signin")).headers.get("content-security-policy")!;
    const second = (await call("/signin")).headers.get("content-security-policy")!;
    const nonce = (policy: string) => /'nonce-([^']+)'/.exec(policy)![1];

    expect(nonce(first)).not.toBe(nonce(second));
    for (const directive of ["default-src 'none'", "style-src-attr 'none'", "frame-ancestors 'none'", "form-action 'self'", "base-uri 'none'", "object-src 'none'", "connect-src 'self'"]) {
      expect(first).toContain(directive);
    }
    expect(first).not.toContain("unsafe-inline");
    expect(first).not.toContain("unsafe-eval");
    expect(first).not.toMatch(/https?:/);
  });

  it("sets the security headers, and no caching, on a redirect as on a page", async () => {
    for (const response of [await call("/"), await call("/signin")]) {
      expect(response.headers.get("x-content-type-options")).toBe("nosniff");
      expect(response.headers.get("referrer-policy")).toBe("same-origin");
      expect(response.headers.get("x-frame-options")).toBe("DENY");
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(response.headers.get("cross-origin-opener-policy")).toBe("same-origin");
    }
  });

  it("does not count the page's own background refresh as use, so a tab left open still times out", async () => {
    const stale = issue(SECRET, nowS() - 120, { sid: "s", iat: nowS() - 120, seen: nowS() - 120 });

    const background = await call("/api/live", stale);
    const action = await call("/", stale);

    expect(background.status).toBe(200);
    expect(background.headers.get("set-cookie")).toBeNull();
    expect(action.headers.get("set-cookie")).toContain(`${COOKIE_NAME}=`);
  });

  it("refuses a session that was signed out, even though its signature is still good", async () => {
    const { revoke } = await import("@/lib/auth/revoked");
    const token = issue(SECRET, nowS());
    expect((await call("/", token)).status).toBe(200);
    const payload = JSON.parse(Buffer.from(token.split(".")[1]!, "base64url").toString()) as { sid: string };

    revoke(payload.sid, nowS());

    expect((await call("/", token)).status).toBe(303);
    expect((await call("/api/live", token)).status).toBe(401);
  });

  it("signs the cookie again only when it has gone stale", async () => {
    const fresh = await call("/", issue(SECRET, nowS()));
    const stale = await call("/", issue(SECRET, nowS() - 120, { sid: "s", iat: nowS() - 120, seen: nowS() - 120 }));

    expect(fresh.headers.get("set-cookie")).toBeNull();
    expect(stale.headers.get("set-cookie")).toContain(`${COOKIE_NAME}=`);
    expect(stale.headers.get("set-cookie")).toContain("HttpOnly");
  });
});
