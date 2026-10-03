import { type NextRequest, NextResponse } from "next/server";
import { config as settings } from "./lib/config";
import { hostAllowed } from "./lib/auth/hosts";
import { COOKIE_NAME, REFRESH_AFTER_S, cookieAttributes, issue, verify } from "./lib/auth/session";
import { ABSOLUTE_LIFETIME_S } from "./lib/auth/session";

/** Runs before every request that is not a static file: puts a fresh nonce CSP and the security
 * headers on the answer, and turns away anyone who is not signed in (the sign-in page, its
 * form's endpoint and the health check are open). The pages and the data functions check the
 * session again: this is the first gate, not the only one. */

const OPEN_PATHS = new Set(["/signin", "/api/signin", "/healthz"]);
const now = () => Math.floor(Date.now() / 1000);

function policy(nonce: string): string {
  const dev = process.env.NODE_ENV === "development";
  return [
    "default-src 'none'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'nonce-${nonce}'`,
    "style-src-attr 'none'",
    "img-src 'self'",
    "font-src 'self'",
    "connect-src 'self'",
    "manifest-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
}

const HEADERS: Record<string, string> = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "X-Frame-Options": "DENY",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  "Cache-Control": "no-store",
};

function secure(response: NextResponse, csp: string): NextResponse {
  response.headers.set("Content-Security-Policy", csp);
  for (const [name, value] of Object.entries(HEADERS)) response.headers.set(name, value);
  return response;
}

export function proxy(request: NextRequest): NextResponse {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = policy(nonce);
  const path = request.nextUrl.pathname;
  // A request sent under a name that is not ours (DNS rebinding) is turned away before anything else.
  if (!hostAllowed(request.headers.get("host"), settings().allowedHosts)) {
    return secure(new NextResponse("Misdirected Request", { status: 421 }), csp);
  }
  const payload = verify(request.cookies.get(COOKIE_NAME)?.value, settings().sessionSecret, now());

  if (!payload && !OPEN_PATHS.has(path)) {
    if (path.startsWith("/api/")) {
      return secure(NextResponse.json({ error: "not signed in" }, { status: 401 }), csp);
    }
    return secure(NextResponse.redirect(new URL("/signin", request.url), 303), csp);
  }

  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", csp);
  const response = secure(NextResponse.next({ request: { headers } }), csp);
  // Keep the idle clock moving, and write the cookie only when it has gone stale.
  if (payload && now() - payload.seen >= REFRESH_AFTER_S) {
    const remaining = Math.max(ABSOLUTE_LIFETIME_S - (now() - payload.iat), 0);
    response.headers.append(
      "Set-Cookie",
      `${COOKIE_NAME}=${issue(settings().sessionSecret, now(), payload)}; ${cookieAttributes(remaining)}`,
    );
  }
  return response;
}

export const config = {
  matcher: [{ source: "/((?!_next/static|_next/image|brand/|fonts/|favicon.ico).*)" }],
};
