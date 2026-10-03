import { Pool, type PoolClient } from "pg";
import { config } from "./config";
import { type AuthedSession, assertAuthed } from "./auth/authed";

/** The dashboard reads through the telemetry reader role, whose read-only and 5 s settings are only
 * session defaults that a session can change. So the dashboard sets its own: every connection starts
 * read-only with a statement timeout, and every query runs in an explicit READ ONLY transaction with
 * `SET LOCAL statement_timeout`. At most three connections (the role allows five). */

export const STATEMENT_TIMEOUT_MS = 4000;
const POOL_SIZE = 3;

type Queryable = Pick<Pool, "connect">;

let pool: Queryable | undefined;

function realPool(): Queryable {
  pool ??= new Pool({
    connectionString: config().databaseUrl,
    max: POOL_SIZE,
    connectionTimeoutMillis: 3000,
    idleTimeoutMillis: 30_000,
    query_timeout: STATEMENT_TIMEOUT_MS + 1000,
    application_name: "ai-gateway-dashboard",
    options: `-c default_transaction_read_only=on -c statement_timeout=${STATEMENT_TIMEOUT_MS} -c idle_in_transaction_session_timeout=10000`,
  });
  return pool;
}

/** For tests: use another pool. */
export function usePool(replacement: Queryable | undefined): void {
  pool = replacement;
}

/** Run `work` in a read-only transaction as the signed-in session. The session is checked here too:
 * nothing reaches the database without one. */
export async function readOnly<T>(session: AuthedSession, work: (client: PoolClient) => Promise<T>): Promise<T> {
  assertAuthed(session);
  const client = await realPool().connect();
  try {
    await client.query("BEGIN READ ONLY");
    await client.query(`SET LOCAL statement_timeout = ${STATEMENT_TIMEOUT_MS}`);
    const result = await work(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw error;
  } finally {
    client.release();
  }
}
