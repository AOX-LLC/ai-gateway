import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";

/** A signed session cookie: v1.<payload>.<signature>. The payload says when it was issued and last
 * seen; the signature is HMAC-SHA256 over it with the session secret. Nothing is stored on the
 * server, so a restart signs nobody out, and changing the secret signs everybody out. */

export const COOKIE_NAME = "__Host-aig_session";
export const ABSOLUTE_LIFETIME_S = 8 * 60 * 60;
export const IDLE_LIFETIME_S = 30 * 60;
/** How stale `seen` may get before the cookie is signed again: bounds the cookie writes. */
export const REFRESH_AFTER_S = 60;

export type SessionPayload = { sid: string; iat: number; seen: number };

const b64 = (value: Buffer | string) => Buffer.from(value).toString("base64url");

function sign(body: string, secret: string): Buffer {
  return createHmac("sha256", secret).update(`v1.${body}`).digest();
}

export function issue(secret: string, now: number, previous?: SessionPayload): string {
  const payload: SessionPayload = previous
    ? { ...previous, seen: now }
    : { sid: randomUUID(), iat: now, seen: now };
  const body = b64(JSON.stringify(payload));
  return `v1.${body}.${b64(sign(body, secret))}`;
}

/** The payload if the token is ours, unmodified, and neither past its absolute nor its idle limit. */
export function verify(token: string | undefined, secret: string, now: number): SessionPayload | undefined {
  if (!token) return undefined;
  const [version, body, signature, extra] = token.split(".");
  if (version !== "v1" || !body || !signature || extra !== undefined) return undefined;
  const expected = sign(body, secret);
  const given = Buffer.from(signature, "base64url");
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) return undefined;
  let payload: unknown;
  try {
    payload = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
  } catch {
    return undefined;
  }
  if (!isPayload(payload)) return undefined;
  if (now - payload.iat > ABSOLUTE_LIFETIME_S || now - payload.seen > IDLE_LIFETIME_S) return undefined;
  if (payload.iat > now + 60 || payload.seen > now + 60) return undefined;
  return payload;
}

function isPayload(value: unknown): value is SessionPayload {
  if (typeof value !== "object" || value === null) return false;
  const { sid, iat, seen } = value as Record<string, unknown>;
  return typeof sid === "string" && Number.isInteger(iat) && Number.isInteger(seen);
}

export function cookieAttributes(maxAgeSeconds: number): string {
  return `Path=/; Max-Age=${maxAgeSeconds}; HttpOnly; Secure; SameSite=Strict`;
}
