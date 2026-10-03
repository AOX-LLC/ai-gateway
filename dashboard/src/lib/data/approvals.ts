import { type AuthedSession, assertAuthed } from "../auth/authed";
import { readOnly } from "../db";
import type { Approval, ApprovalStatus, Approvals } from "./types";

const COLUMNS = `
  id, action, namespace, client_name,
  CASE WHEN status = 'pending' AND expires_at <= now() THEN 'expired' ELSE status END AS status,
  to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS created_at,
  to_char(expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS expires_at,
  to_char(resolved_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS resolved_at,
  resolved_by_name`;

const PENDING = `SELECT ${COLUMNS} FROM dash_approvals
  WHERE status = 'pending' AND expires_at > now() ORDER BY created_at LIMIT 20`;
const RECENT = `SELECT ${COLUMNS} FROM dash_approvals
  WHERE NOT (status = 'pending' AND expires_at > now()) ORDER BY created_at DESC LIMIT 8`;

const STATUSES: readonly ApprovalStatus[] = ["pending", "approved", "rejected", "expired", "consumed", "cancelled"];

export async function getApprovals(session: AuthedSession): Promise<Approvals> {
  assertAuthed(session);
  return readOnly(session, async (client) => ({
    pending: (await client.query(PENDING)).rows.map(toApproval),
    recent: (await client.query(RECENT)).rows.map(toApproval),
  }));
}

function toApproval(row: Record<string, unknown>): Approval {
  const status = STATUSES.find((candidate) => candidate === row.status) ?? "cancelled";
  const optional = (key: string) => (row[key] === null || row[key] === undefined ? null : String(row[key]));
  return {
    id: String(row.id),
    tool: String(row.action),
    namespace: optional("namespace") ?? "unknown",
    clientName: optional("client_name"),
    status,
    createdAt: String(row.created_at),
    expiresAt: String(row.expires_at),
    resolvedAt: optional("resolved_at"),
    decidedBy: optional("resolved_by_name"),
  };
}
