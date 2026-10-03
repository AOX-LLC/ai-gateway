import { formatInt, formatMs, formatPercent, formatRate, windowLabel } from "@/lib/format";
import type { Kpis } from "@/lib/data/types";
import { Icon } from "./Icon";
import { ErrorState, SkeletonKpi, StaleNote } from "./States";

type Props = { kpis: Kpis | null; error: string | null; stale: boolean; since: string | null; loading: boolean };

export function KpiTiles({ kpis, error, stale, since, loading }: Props) {
  if (!kpis && loading) {
    return (
      <section className="kpi-grid" aria-label="Key metrics" aria-busy="true">
        {[0, 1, 2, 3].map((i) => (
          <SkeletonKpi key={i} />
        ))}
      </section>
    );
  }
  if (!kpis) {
    return (
      <section aria-label="Key metrics">
        <ErrorState message={error ?? "No data."} />
      </section>
    );
  }
  const window = windowLabel(kpis.windowSeconds);
  return (
    <section aria-label="Key metrics">
      {stale && error ? <StaleNote since={since} error={error} /> : null}
      <div className="kpi-grid">
        <article className="pui-panel pui-kpi" aria-label="Requests">
          <div className="pui-kpi-top">
            <span className="pui-eyebrow">Requests</span>
          </div>
          <div className="pui-kpi-value">
            {formatInt(kpis.requests)}
          </div>
          <div className="pui-kpi-foot">
            {kpis.requests === 0 ? `No tool calls in the last ${window}` : `${formatRate(kpis.perMinute)} a minute over the last ${window}`}
          </div>
        </article>
        <article className="pui-panel pui-kpi" aria-label="p95 latency">
          <div className="pui-kpi-top">
            <span className="pui-eyebrow">p95 latency</span>
          </div>
          <div className="pui-kpi-value">
            {kpis.p95Ms === null ? "—" : formatMs(kpis.p95Ms)}
            {kpis.p95Ms === null ? null : <span className="pui-kpi-unit">ms</span>}
          </div>
          <div className="pui-kpi-foot">Slowest 5% of tool calls, gateway time</div>
        </article>
        <article className="pui-panel pui-kpi" aria-label="Success rate">
          <div className="pui-kpi-top">
            <span className="pui-eyebrow">Success rate</span>
            {kpis.forwarded - kpis.succeeded > 0 ? (
              <span className="pui-badge pui-badge--warning">
                <Icon name="alert" size="sm" />
                {formatInt(kpis.forwarded - kpis.succeeded)} upstream {kpis.forwarded - kpis.succeeded === 1 ? "error" : "errors"}
              </span>
            ) : null}
          </div>
          <div className="pui-kpi-value">
            {kpis.successRate === null ? "—" : formatPercent(kpis.successRate)}
            {kpis.successRate === null ? null : <span className="pui-kpi-unit">%</span>}
          </div>
          <div className="pui-kpi-foot">
            {kpis.forwarded === 0 ? "Nothing was forwarded" : `${formatInt(kpis.succeeded)} of ${formatInt(kpis.forwarded)} forwarded calls answered`}
          </div>
        </article>
        <article className="pui-panel pui-kpi" aria-label="Blocked">
          <div className="pui-kpi-top">
            <span className="pui-eyebrow">Blocked</span>
            {kpis.wouldBlock > 0 ? (
              <span className="pui-badge pui-badge--warning">
                <Icon name="alert" size="sm" />
                {formatInt(kpis.wouldBlock)} would block
              </span>
            ) : null}
          </div>
          <div className="pui-kpi-value">{formatInt(kpis.blocked)}</div>
          <div className="pui-kpi-foot">
            {formatInt(kpis.authFailures)} failed {kpis.authFailures === 1 ? "sign-in" : "sign-ins"} not counted
          </div>
        </article>
      </div>
    </section>
  );
}
