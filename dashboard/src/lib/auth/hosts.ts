/** The Host header names where a request was sent. A page on another site can make a browser send
 * requests here under its own name (DNS rebinding: its name is made to point at this address), and
 * then the Host and the Origin both say that name. Refusing every Host that is not one of ours stops
 * that before anything else is looked at. A pattern is `host:port`, where the port may be `*`. */

export const DEFAULT_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"] as const;

export function parseHosts(value: string | undefined): string[] {
  const patterns = (value ?? "")
    .split(",")
    .map((part) => part.trim().toLowerCase())
    .filter((part) => part.length > 0);
  return patterns.length > 0 ? patterns : [...DEFAULT_ALLOWED_HOSTS];
}

export function hostAllowed(host: string | null | undefined, patterns: readonly string[]): boolean {
  if (!host) return false;
  const given = host.toLowerCase();
  return patterns.some((pattern) => {
    if (pattern.endsWith(":*")) {
      const name = pattern.slice(0, -2);
      return given === name || (given.startsWith(`${name}:`) && /^\d{1,5}$/.test(given.slice(name.length + 1)));
    }
    return given === pattern;
  });
}
