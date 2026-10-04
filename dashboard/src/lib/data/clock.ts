import { type AuthedSession, assertAuthed } from "../auth/authed";
import { config } from "../config";
import { readOnly } from "../db";

/** What "now" means in the queries. Normally the database's clock. In a demo stack
 * (DASHBOARD_DEMO=1) it is the time of the newest recorded request, so the seeded state does not age:
 * the ranges, the charts and the approval queue look the same whenever someone looks. The text is one
 * of two constants, never built from input. */
export function nowSql(): string {
  return config().demo ? "(SELECT max(ts) FROM telemetry.dash_requests)" : "now()";
}

/** The instant the panels are as of, as ISO 8601 UTC. */
export async function getClock(session: AuthedSession): Promise<string> {
  assertAuthed(session);
  const { rows } = await readOnly(session, (client) =>
    client.query(`SELECT to_char(${nowSql()} AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS at`),
  );
  const at = (rows[0] as { at: string | null } | undefined)?.at;
  return at ?? new Date().toISOString();
}
