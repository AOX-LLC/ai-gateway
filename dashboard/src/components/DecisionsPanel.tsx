import { clock, formatMs, words } from "@/lib/format";
import type { Decision, DecisionsPage } from "@/lib/data/types";
import { Icon } from "./Icon";
import { EmptyState, ErrorState, SkeletonLines, StaleNote } from "./States";

export type DecisionsView = {
  page: DecisionsPage | null;
  error: string | null;
  stale: boolean;
  since: string | null;
  loading: boolean;
  /** 0 is the live first page; each older page is one more. */
  index: number;
};

type Props = { view: DecisionsView; onOlder: () => void; onNewer: () => void; rangeLabel: string };

type Look = { rail: "danger" | "warning" | "success" | "pending"; badge: string; icon: "ban" | "check" | "alert" | "clock"; label: string };

/** How one decision looks: a rail colour, a badge class, an icon and a word, so none depends on colour. */
export function look(decision: Decision): Look {
  if (decision.outcome === "blocked") {
    if (decision.denyCode === "approval_pending") {
      return { rail: "pending", badge: "pui-badge--pending", icon: "clock", label: "Awaiting approval" };
    }
    return { rail: "danger", badge: "pui-badge--danger", icon: "ban", label: "Blocked" };
  }
  if (decision.upstreamStatus !== null && decision.upstreamStatus !== "ok") {
    return { rail: "warning", badge: "pui-badge--warning", icon: "alert", label: "Upstream error" };
  }
  return { rail: "success", badge: "pui-badge--success", icon: "check", label: "Forwarded" };
}

function Row({ decision }: { decision: Decision }) {
  const shown = look(decision);
  return (
    <tr>
      <td className="pui-rail" data-status={shown.rail} />
      <td className="pui-mono">{clock(decision.ts)}</td>
      <td className="pui-mono">{decision.clientName}</td>
      <td className="pui-mono">{decision.tool}</td>
      <td>
        <span className="cell-badges">
          <span className={`pui-badge ${shown.badge}`}>
            <Icon name={shown.icon} size="sm" />
            {shown.label}
          </span>
          {decision.wouldBlock.length > 0 ? (
            <span className="pui-badge pui-badge--warning">
              <Icon name="alert" size="sm" />
              Would block: {decision.wouldBlock.map(words).join(", ")}
            </span>
          ) : null}
        </span>
      </td>
      <td>{decision.blockedBy ? words(decision.blockedBy) : <span className="pui-muted">—</span>}</td>
      <td className="pui-mono pui-muted">{decision.denyCode ?? "—"}</td>
      <td className="pui-num">{decision.durationMs === null ? "—" : `${formatMs(decision.durationMs)} ms`}</td>
    </tr>
  );
}

export function DecisionsPanel({ view, onOlder, onNewer, rangeLabel }: Props) {
  const { page } = view;
  return (
    <section className="pui-panel" aria-labelledby="decisions-title" id="decisions">
      <div className="pui-panel-header">
        <div className="pui-panel-heading">
          <h2 className="pui-panel-title" id="decisions-title">
            Recent decisions
          </h2>
          <span className="pui-panel-sub">Every tool call, newest first · last {rangeLabel} · times in UTC</span>
        </div>
        {view.stale && view.error ? <StaleNote since={view.since} error={view.error} /> : null}
      </div>
      {!page && view.loading ? <SkeletonLines rows={6} /> : null}
      {!page && !view.loading ? <ErrorState message={view.error ?? "No data."} /> : null}
      {page && view.loading ? <SkeletonLines rows={6} /> : null}
      {page && !view.loading && page.decisions.length === 0 ? (
        <EmptyState icon="list" title="No tool calls in this window">
          Calls appear here as clients make them, whether the gateway forwarded them or stopped them.
        </EmptyState>
      ) : null}
      {page && !view.loading && page.decisions.length > 0 ? (
        <>
          <div className="pui-table-wrap" role="region" aria-label="Recent decisions" tabIndex={0}>
            <table className="pui-table">
              <thead>
                <tr>
                  <th className="pui-rail">
                    <span className="pui-sr-only">Status</span>
                  </th>
                  <th>Time</th>
                  <th>Client</th>
                  <th>Tool</th>
                  <th>Outcome</th>
                  <th>Layer</th>
                  <th>Code</th>
                  <th className="pui-num">Time taken</th>
                </tr>
              </thead>
              <tbody>
                {page.decisions.map((decision) => (
                  <Row key={decision.requestId} decision={decision} />
                ))}
              </tbody>
            </table>
          </div>
          <div className="pui-table-footer">
            <span>
              {page.decisions.length} {page.decisions.length === 1 ? "call" : "calls"}
              {view.index > 0 ? ` · page ${view.index + 1}` : ""}
            </span>
            <span className="pager">
              <button type="button" className="pui-btn pui-btn--sm" onClick={onNewer} disabled={view.index === 0}>
                <Icon name="left" size="sm" />
                Newer
              </button>
              <button type="button" className="pui-btn pui-btn--sm" onClick={onOlder} disabled={page.nextCursor === null}>
                Older
                <Icon name="right" size="sm" />
              </button>
            </span>
          </div>
        </>
      ) : null}
    </section>
  );
}
