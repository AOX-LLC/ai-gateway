import { describe, expect, it } from "vitest";
import { DEFAULT_ALLOWED_HOSTS, hostAllowed, parseHosts } from "@/lib/auth/hosts";

describe("the allowed hosts", () => {
  it("answers to loopback on any port by default", () => {
    for (const host of ["127.0.0.1:4400", "localhost:4400", "LOCALHOST:9", "127.0.0.1", "[::1]:4400"]) {
      expect(hostAllowed(host, DEFAULT_ALLOWED_HOSTS), host).toBe(true);
    }
  });

  it.each([
    [null],
    [undefined],
    [""],
    ["evil.example"],
    ["127.0.0.1.evil.example"],
    ["localhost.evil.example:4400"],
    ["evil.example:4400"],
    ["127.0.0.1:abc"],
    ["127.0.0.1:44000000"],
    ["127.0.0.1@evil.example"],
  ])("refuses %s", (host) => {
    expect(hostAllowed(host, DEFAULT_ALLOWED_HOSTS)).toBe(false);
  });

  it("takes a list from the settings, with exact and wildcard ports, and falls back to loopback when empty", () => {
    const patterns = parseHosts(" Dash.Example:8443 , tailnet-host:* ");

    expect(patterns).toEqual(["dash.example:8443", "tailnet-host:*"]);
    expect(hostAllowed("dash.example:8443", patterns)).toBe(true);
    expect(hostAllowed("dash.example:9", patterns)).toBe(false);
    expect(hostAllowed("tailnet-host:4400", patterns)).toBe(true);
    expect(parseHosts("")).toEqual([...DEFAULT_ALLOWED_HOSTS]);
    expect(parseHosts(undefined)).toEqual([...DEFAULT_ALLOWED_HOSTS]);
  });
});
