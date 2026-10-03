import Link from "next/link";
import type { ReactNode } from "react";
import { Icon } from "./Icon";
import { ThemeToggle } from "./ThemeToggle";

const RANGES = ["1h", "24h", "7d", "30d"] as const;

type Props = { range: (typeof RANGES)[number]; theme: "dark" | "light"; sampleData: boolean; children: ReactNode };

/** The frame of the UI system: sidebar with the AOX logo top left, top bar, content column. */
export function Shell({ range, theme, sampleData, children }: Props) {
  return (
    <div className="pui-shell">
      <aside className="pui-sidebar">
        <Link className="pui-brand" href="/" prefetch={false}>
          {/* eslint-disable-next-line @next/next/no-img-element -- the logo is a fixed PNG; image optimisation is off */}
          <img className="pui-logo pui-logo--light" src="/brand/aox-logo-black.png" alt="AOX" height={20} />
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img className="pui-logo pui-logo--dark" src="/brand/aox-logo-white.png" alt="AOX" height={20} />
          <span className="pui-brand-divider" aria-hidden="true" />
          <span className="pui-brand-app">Gateway</span>
        </Link>
        <nav className="pui-nav" aria-label="Primary">
          <div className="pui-nav-group">
            <div className="pui-eyebrow">Monitor</div>
            <a className="pui-nav-item" href="#overview" aria-current="page">
              <Icon name="activity" />
              Overview
            </a>
            <a className="pui-nav-item" href="#decisions">
              <Icon name="list" />
              Decisions
            </a>
          </div>
          <div className="pui-nav-group">
            <div className="pui-eyebrow">Govern</div>
            <a className="pui-nav-item" href="#approvals">
              <Icon name="clock" />
              Approvals
            </a>
          </div>
        </nav>
        <div className="pui-sidebar-footer">
          <span className="pui-mono">Read-only</span>
          <span>Views of the telemetry and approvals only</span>
        </div>
      </aside>
      <div className="pui-main">
        <header className="pui-topbar">
          <div className="pui-topbar-group">
            <span className="crumb-strong">Harborline Supply Co. (fictional)</span>
            <span className="pui-crumb-sep" aria-hidden="true">
              /
            </span>
            <span>Overview</span>
            {sampleData ? <span className="pui-sample-pill">Sample data</span> : null}
          </div>
          <div className="pui-topbar-group">
            <nav className="range-seg" aria-label="Time range">
              {RANGES.map((value) => (
                <Link key={value} href={`/?range=${value}`} prefetch={false} aria-current={value === range ? "true" : undefined}>
                  {value}
                </Link>
              ))}
            </nav>
            <ThemeToggle initial={theme} />
            <form className="topbar-form" method="post" action="/api/signout">
              <button type="submit" className="pui-btn pui-btn--icon" aria-label="Sign out">
                <Icon name="logout" />
              </button>
            </form>
          </div>
        </header>
        <main className="pui-content" id="overview">
          <div className="pui-page-header">
            <div>
              <div className="pui-eyebrow">04 / Overview</div>
              <h1 className="pui-page-title">Gateway overview</h1>
            </div>
          </div>
          {children}
        </main>
      </div>
    </div>
  );
}
