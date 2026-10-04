import "server-only";
import type { AuthedSession } from "../auth/authed";
import { AuthError } from "../auth/authed";
import { getApprovals, getCharts, getClock, getDecisions, getKpis } from "./index";
import type { Range } from "./ranges";
import type { Overview, Result } from "./types";

const UNREADABLE = "The database did not answer. The last good data stays until it does.";

async function settle<T>(panel: string, read: () => Promise<T>): Promise<Result<T>> {
  try {
    return { ok: true, data: await read() };
  } catch (error) {
    if (error instanceof AuthError) throw error;
    // The error's name and Postgres's error code, never its message: that can quote a query or a value.
    const code = (error as { code?: unknown }).code;
    console.error(`dashboard: ${panel} could not be read: ${error instanceof Error ? error.name : "error"} ${typeof code === "string" ? code : ""}`.trim());
    return { ok: false, error: UNREADABLE };
  }
}

/** The three panels' data. Each panel can fail on its own and says so; a missing session fails all. */
export async function getOverview(session: AuthedSession, range: Range): Promise<Overview> {
  const [at, kpis, charts, decisions, approvals] = await Promise.all([
    getClock(session).catch((error: unknown) => {
      if (error instanceof AuthError) throw error;
      console.error(`dashboard: the clock could not be read: ${error instanceof Error ? error.name : "error"}`);
      return new Date().toISOString();
    }),
    settle("kpis", () => getKpis(session, range)),
    settle("charts", () => getCharts(session, range)),
    settle("decisions", () => getDecisions(session, { range })),
    settle("approvals", () => getApprovals(session)),
  ]);
  return { at, kpis, charts, decisions, approvals };
}
