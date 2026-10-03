import { describe, expect, it } from "vitest";
import {
  ABSOLUTE_LIFETIME_S,
  COOKIE_NAME,
  IDLE_LIFETIME_S,
  cookieAttributes,
  issue,
  verify,
} from "@/lib/auth/session";

const SECRET = "s".repeat(40);
const NOW = 1_800_000_000;

describe("the session cookie", () => {
  it("is accepted when it is ours and recent", () => {
    const payload = verify(issue(SECRET, NOW), SECRET, NOW + 10);

    expect(payload).toMatchObject({ iat: NOW, seen: NOW });
  });

  it("is refused with another secret, an edited payload or a cut signature", () => {
    const token = issue(SECRET, NOW);
    const [version, body, signature] = token.split(".") as [string, string, string];
    const forged = Buffer.from(JSON.stringify({ sid: "x", iat: NOW, seen: NOW + 99 })).toString("base64url");

    expect(verify(token, "t".repeat(40), NOW)).toBeUndefined();
    expect(verify(`${version}.${forged}.${signature}`, SECRET, NOW)).toBeUndefined();
    expect(verify(`${version}.${body}.${signature.slice(0, -2)}`, SECRET, NOW)).toBeUndefined();
    expect(verify(`${version}.${body}.${signature}.x`, SECRET, NOW)).toBeUndefined();
    expect(verify(undefined, SECRET, NOW)).toBeUndefined();
    expect(verify("garbage", SECRET, NOW)).toBeUndefined();
  });

  it("ends after the idle limit, and after the absolute limit however recently it was used", () => {
    const token = issue(SECRET, NOW);

    expect(verify(token, SECRET, NOW + IDLE_LIFETIME_S)).toBeDefined();
    expect(verify(token, SECRET, NOW + IDLE_LIFETIME_S + 1)).toBeUndefined();

    const busy = issue(SECRET, NOW + ABSOLUTE_LIFETIME_S - 5, { sid: "s", iat: NOW, seen: NOW });
    expect(verify(busy, SECRET, NOW + ABSOLUTE_LIFETIME_S)).toBeDefined();
    expect(verify(busy, SECRET, NOW + ABSOLUTE_LIFETIME_S + 1)).toBeUndefined();
  });

  it("is refused when it claims to be from the future", () => {
    expect(verify(issue(SECRET, NOW + 3600), SECRET, NOW)).toBeUndefined();
  });

  it("is a __Host- cookie that scripts cannot read and other sites cannot send", () => {
    const attributes = cookieAttributes(60);

    expect(COOKIE_NAME.startsWith("__Host-")).toBe(true);
    for (const wanted of ["Path=/", "HttpOnly", "Secure", "SameSite=Strict"]) expect(attributes).toContain(wanted);
    expect(attributes).not.toContain("Domain");
  });
});
