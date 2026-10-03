import Link from "next/link";
import { Icon } from "@/components/Icon";

/** Next's own 404 page uses inline styles, which the CSP forbids; this one uses the system's classes. */
export default function NotFound() {
  return (
    <div className="signin-wrap">
      <div className="pui-alert pui-alert--info signin-card" role="status">
        <Icon name="info" />
        <div className="pui-alert-body">
          <div className="pui-alert-title">Page not found</div>
          <div className="pui-alert-text">There is nothing at this address.</div>
          <div className="pui-alert-actions">
            <Link className="pui-btn pui-btn--sm" href="/" prefetch={false}>
              Back to the overview
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
