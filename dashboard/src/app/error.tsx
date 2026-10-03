"use client";

import { Icon } from "@/components/Icon";

/** The page itself failed to render: the database is down or something is wrong with this
 * dashboard. Nothing about the failure is shown, only that it happened and how to try again. */
export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <div className="signin-wrap">
      <div className="pui-alert pui-alert--danger signin-card" role="alert">
        <Icon name="alert" />
        <div className="pui-alert-body">
          <div className="pui-alert-title">The dashboard could not load</div>
          <div className="pui-alert-text">Something went wrong reading the data. Nothing was changed: this dashboard only reads.</div>
          <div className="pui-alert-actions">
            <button type="button" className="pui-btn pui-btn--sm" onClick={reset}>
              Try again
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
