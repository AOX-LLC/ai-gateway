import { redirect } from "next/navigation";
import { Icon } from "@/components/Icon";
import { config } from "@/lib/config";
import { currentSession } from "@/lib/auth/server";

export const dynamic = "force-dynamic";

const MESSAGES: Record<string, string> = {
  denied: "That password did not match. Enter the admin password again.",
  wait: "Too many attempts. Wait a minute, then try again.",
  disabled: "No admin password is set for this dashboard. Run scripts/set_dashboard_password.py, then restart it.",
};

export default async function SignIn({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  if (await currentSession()) redirect("/");
  const params = await searchParams;
  const code = typeof params.error === "string" ? params.error : undefined;
  const disabled = config().adminPasswordHash === "";
  const message = disabled ? MESSAGES.disabled : code && Object.hasOwn(MESSAGES, code) ? MESSAGES[code] : undefined;
  return (
    <main className="signin-wrap">
      <div className="pui-panel signin-card">
        <div className="signin-brand">
          {/* eslint-disable-next-line @next/next/no-img-element -- fixed PNG, image optimisation is off */}
          <img className="pui-logo pui-logo--light" src="/brand/aox-logo-black.png" alt="AOX" height={20} />
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img className="pui-logo pui-logo--dark" src="/brand/aox-logo-white.png" alt="AOX" height={20} />
          <span className="pui-brand-divider" aria-hidden="true" />
          <span className="pui-brand-app">Gateway</span>
        </div>
        <form className="signin-form" method="post" action="/api/signin">
          <div>
            <div className="pui-eyebrow">Admin</div>
            <h1 className="pui-page-title">Sign in</h1>
          </div>
          {message ? (
            <div className="pui-alert pui-alert--danger" role="alert">
              <Icon name="alert" />
              <div className="pui-alert-body">
                <div className="pui-alert-text">{message}</div>
              </div>
            </div>
          ) : null}
          <div className="pui-field">
            <label className="pui-label" htmlFor="password">
              Password
            </label>
            <input className="pui-input" id="password" name="password" type="password" autoComplete="current-password" required autoFocus disabled={disabled} />
          </div>
          <button type="submit" className="pui-btn pui-btn--primary" disabled={disabled}>
            Sign in
          </button>
          <p className="pui-hint">Read-only dashboard for the Harborline Supply Co. demo (fictional).</p>
        </form>
      </div>
    </main>
  );
}
