import "server-only";
import type { AuthedSession } from "../auth/authed";
import { AuthError } from "../auth/authed";
import { getApprovals, getDecisions, getKpis } from "./index";
import type { Range } from "./ranges";
import type { Overview, Result } from "./types";

const UNREADABLE = "The database did not answer. The last good data stays until it does.";

async function settle<T>(panel: string, read: () => Promise<T>): Promise<Result<T>> {
  try {
    return { ok: true, data: await read() };
  } catch (error) {
    if (error instanceof AuthError) throw error;
    console.error(`dashboard: ${panel} could not be read: ${error instanceof Error ? error.name : "error"}`);
    return { ok: false, error: UNREADABLE };
  }
}

/** The three panels' data. Each panel can fail on its own and says so; a missing session fails all. */
export async function getOverview(session: AuthedSession, range: Range): Promise<Overview> {
  const [kpis, decisions, approvals] = await Promise.all([
    settle("kpis", () => getKpis(session, range)),
    settle("decisions", () => getDecisions(session, { range })),
    settle("approvals", () => getApprovals(session)),
  ]);
  return { at: new Date().toISOString(), kpis, decisions, approvals };
}
