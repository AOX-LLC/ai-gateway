import { clock, dayAndMinute, words } from "@/lib/format";
import type { Approval, Approvals } from "@/lib/data/types";
import { Icon } from "./Icon";
import { EmptyState, ErrorState, SkeletonLines, StaleNote } from "./States";

type Props = { approvals: Approvals | null; error: string | null; stale: boolean; since: string | null; loading: boolean };

function decidedBadge(approval: Approval): { className: string; icon: "check" | "ban" | "clock"; label: string } {
  switch (approval.status) {
    case "approved":
      return { className: "pui-badge--success", icon: "check", label: "Approved" };
    case "consumed":
      return { className: "pui-badge--success", icon: "check", label: "Approved and used" };
    case "rejected":
      return { className: "pui-badge--danger", icon: "ban", label: "Rejected" };
    case "expired":
      return { className: "pui-badge--neutral", icon: "clock", label: "Expired" };
    default:
      return { className: "pui-badge--neutral", icon: "ban", label: words(approval.status) };
  }
}

function PendingItem({ approval }: { approval: Approval }) {
  return (
    <li className="queue-item" data-pending="true">
      <div className="queue-row">
        <span className="pui-mono">{approval.tool}</span>
        <span className="pui-badge pui-badge--pending">
          <Icon name="clock" size="sm" />
          Pending
        </span>
      </div>
      <div className="queue-meta">
        Asked by <span className="pui-mono">{approval.clientName ?? "an unregistered client"}</span> for the{" "}
        <span className="pui-mono">{approval.namespace}</span> server
      </div>
      <div className="queue-meta">
        Asked {clock(approval.createdAt)} · expires {clock(approval.expiresAt)} UTC
      </div>
    </li>
  );
}

function DecidedItem({ approval }: { approval: Approval }) {
  const badge = decidedBadge(approval);
  return (
    <li className="queue-item">
      <div className="queue-row">
        <span className="pui-mono">{approval.tool}</span>
        <span className={`pui-badge ${badge.className}`}>
          <Icon name={badge.icon} size="sm" />
          {badge.label}
        </span>
      </div>
      <div className="queue-meta">
        <span className="pui-mono">{approval.clientName ?? "an unregistered client"}</span> · {approval.namespace} server
        {approval.decidedBy ? ` · decided by ${approval.decidedBy}` : ""}
      </div>
      <div className="queue-meta">{dayAndMinute(approval.resolvedAt ?? approval.createdAt)} UTC</div>
    </li>
  );
}

export function ApprovalsPanel({ approvals, error, stale, since, loading }: Props) {
  const waiting = approvals?.pending.length ?? 0;
  return (
    <section className="pui-panel" aria-labelledby="approvals-title" id="approvals">
      <div className="pui-panel-header">
        <div className="pui-panel-heading">
          <h2 className="pui-panel-title" id="approvals-title">
            Approval queue
          </h2>
          <span className="pui-panel-sub">Write actions wait here for a person</span>
        </div>
        {approvals ? (
          <span className={waiting > 0 ? "pui-badge pui-badge--pending" : "pui-badge pui-badge--neutral"}>
            {waiting > 0 ? <Icon name="clock" size="sm" /> : null}
            {waiting} waiting
          </span>
        ) : null}
        {stale && error ? <StaleNote since={since} error={error} /> : null}
      </div>
      <p className="panel-note">
        Read-only. Decide with <span className="pui-mono">gateway-approver</span>; nothing is sent until a person approves.
      </p>
      {!approvals && loading ? <SkeletonLines rows={5} /> : null}
      {!approvals && !loading ? <ErrorState message={error ?? "No data."} /> : null}
      {approvals ? (
        <div className="queue-body">
          <div className="queue-column">
            <div className="queue-section pui-eyebrow">Waiting for a person</div>
            {waiting === 0 ? (
              <EmptyState icon="inbox" title="Queue clear">
                Every decision is in the audit log.
              </EmptyState>
            ) : (
              <ul aria-label="Waiting for a person">
                {approvals.pending.map((approval) => (
                  <PendingItem key={approval.id} approval={approval} />
                ))}
              </ul>
            )}
          </div>
          {approvals.recent.length > 0 ? (
            <div className="queue-column">
              <div className="queue-section pui-eyebrow">Recently decided</div>
              <ul aria-label="Recently decided">
                {approvals.recent.map((approval) => (
                  <DecidedItem key={approval.id} approval={approval} />
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
