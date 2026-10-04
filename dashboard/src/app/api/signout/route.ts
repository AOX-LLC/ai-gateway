import { NextResponse } from "next/server";
import { seeOther } from "@/lib/redirect";
import { isSameOrigin } from "@/lib/auth/origin";
import { config } from "@/lib/config";
import { revoke } from "@/lib/auth/revoked";
import { COOKIE_NAME, cookieAttributes, verify } from "@/lib/auth/session";

export const dynamic = "force-dynamic";

function cookieValue(request: Request): string | undefined {
  const match = new RegExp(`(?:^|;\\s*)${COOKIE_NAME}=([^;]*)`).exec(request.headers.get("cookie") ?? "");
  return match?.[1];
}

export function POST(request: Request): NextResponse {
  if (!isSameOrigin(request.headers)) return new NextResponse("Forbidden", { status: 403 });
  // Forget the session on the server too: deleting the cookie in the browser does not stop a copy of it.
  const now = Math.floor(Date.now() / 1000);
  const payload = verify(cookieValue(request), config().sessionSecret, now);
  if (payload) revoke(payload.sid, now);
  const response = seeOther("/signin");
  response.headers.append("Set-Cookie", `${COOKIE_NAME}=; ${cookieAttributes(0)}`);
  return response;
}
