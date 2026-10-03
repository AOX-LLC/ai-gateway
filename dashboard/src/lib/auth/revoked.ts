/** Sessions that were signed out, remembered until they would have expired anyway. A signed cookie
 * keeps its signature after it is deleted from the browser, so without this a copied cookie would
 * outlive the sign-out. Kept on `globalThis` so the proxy and the route handlers, which Next may
 * bundle separately inside one process, see one list. It is in memory: a restart forgets it, and a
 * cookie signed out before the restart is good again until it expires. */

import { ABSOLUTE_LIFETIME_S } from "./session";

const KEY = Symbol.for("ai-gateway.dashboard.revoked-sessions");
type Registry = Map<string, number>;

function registry(): Registry {
  const holder = globalThis as unknown as Record<symbol, Registry | undefined>;
  return (holder[KEY] ??= new Map());
}

/** Remember that this session is signed out. `now` is seconds. */
export function revoke(sid: string, now: number): void {
  const sessions = registry();
  for (const [id, until] of sessions) if (until <= now) sessions.delete(id);
  sessions.set(sid, now + ABSOLUTE_LIFETIME_S + 60);
}

export function isRevoked(sid: string): boolean {
  return registry().has(sid);
}
