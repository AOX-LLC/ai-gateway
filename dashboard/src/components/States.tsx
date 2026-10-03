import type { ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

/** The four states every panel can be in besides showing data: loading, empty, failed, and not
 * active yet. Status is always an icon and words, never colour alone. */

export function SkeletonLines({ rows = 4 }: { rows?: number }) {
  const widths = ["sk-w-90", "sk-w-60", "sk-w-100", "sk-w-40", "sk-w-90", "sk-w-60"];
  return (
    <div className="sk-stack" role="status" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className={`pui-skeleton sk-h-14 ${widths[i % widths.length]}`} />
      ))}
    </div>
  );
}

export function SkeletonKpi() {
  return (
    <div className="pui-panel pui-kpi" role="status" aria-busy="true" aria-label="Loading">
      <span className="pui-skeleton sk-h-14 sk-w-40" />
      <span className="pui-skeleton sk-h-30 sk-w-60" />
      <span className="pui-skeleton sk-h-14 sk-w-90" />
    </div>
  );
}

export function EmptyState({ icon, title, children }: { icon: IconName; title: string; children?: ReactNode }) {
  return (
    <div className="pui-empty">
      <span className="pui-empty-icon">
        <Icon name={icon} size="lg" />
      </span>
      <div className="pui-empty-title">{title}</div>
      {children ? <div className="pui-empty-text">{children}</div> : null}
    </div>
  );
}

export function ErrorState({ message }: { message: string }) {
  return (
    <div className="pui-alert pui-alert--danger" role="alert">
      <Icon name="alert" />
      <div className="pui-alert-body">
        <div className="pui-alert-title">Could not read this panel</div>
        <div className="pui-alert-text">{message} It tries again every 15 seconds.</div>
      </div>
    </div>
  );
}

/** Last good data stays; this says it is old and why. */
export function StaleNote({ since, error }: { since: string | null; error: string }) {
  return (
    <span className="pui-badge pui-badge--warning stale" role="status" title={error}>
      <Icon name="alert" size="sm" />
      {since ? `Not updated since ${since} UTC` : "Not updated"}
    </span>
  );
}

export function NotActive({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="pui-alert pui-alert--info" role="status">
      <Icon name="info" />
      <div className="pui-alert-body">
        <div className="pui-alert-title">{title}</div>
        <div className="pui-alert-text">{children}</div>
      </div>
    </div>
  );
}
