/** A request that changes state must come from this site: its Origin names the host it was sent to.
 * SameSite=Strict on the cookie is the first defence; this is the second. */
export function isSameOrigin(headers: Headers): boolean {
  const host = headers.get("host");
  const origin = headers.get("origin");
  if (!host || !origin) return false;
  try {
    if (new URL(origin).host !== host) return false;
  } catch {
    return false;
  }
  const site = headers.get("sec-fetch-site");
  return site === null || site === "same-origin";
}
