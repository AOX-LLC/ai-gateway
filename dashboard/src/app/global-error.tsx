"use client";

import "@/styles/portfolio-ui.css";
import "@/styles/portfolio-ui-components.css";
import "./app.css";

/** The last resort, when even the layout failed. It replaces Next's prebuilt page, whose scripts carry
 * no nonce and so cannot run under the CSP. A plain link reloads: nothing here needs a script. */
export default function GlobalError() {
  return (
    <html lang="en" data-theme="dark" data-accent="verdigris" data-density="compact">
      <body>
        <div className="signin-wrap">
          <div className="pui-alert pui-alert--danger signin-card" role="alert">
            <div className="pui-alert-body">
              <div className="pui-alert-title">The dashboard could not load</div>
              <div className="pui-alert-text">Nothing was changed: this dashboard only reads.</div>
              <div className="pui-alert-actions">
                {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- a full reload is the point */}
                <a className="pui-btn pui-btn--sm" href="/">
                  Reload
                </a>
              </div>
            </div>
          </div>
        </div>
      </body>
    </html>
  );
}
