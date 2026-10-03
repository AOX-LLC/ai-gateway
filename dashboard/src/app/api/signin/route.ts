import { NextResponse } from "next/server";
import { config } from "@/lib/config";
import { isSameOrigin } from "@/lib/auth/origin";
import { ABSOLUTE_LIFETIME_S, COOKIE_NAME, cookieAttributes } from "@/lib/auth/session";
import { attemptSignIn } from "@/lib/auth/signin";
import { signInThrottle } from "@/lib/auth/throttle";

export const dynamic = "force-dynamic";

function back(request: Request, query: string): NextResponse {
  return NextResponse.redirect(new URL(`/signin${query}`, request.url), 303);
}

export async function POST(request: Request): Promise<NextResponse> {
  if (!isSameOrigin(request.headers)) return new NextResponse("Forbidden", { status: 403 });
  const form = await request.formData().catch(() => null);
  const password = form?.get("password");
  const settings = config();
  const result = await attemptSignIn(typeof password === "string" ? password : "", {
    hash: settings.adminPasswordHash,
    secret: settings.sessionSecret,
    throttle: signInThrottle,
    now: () => Math.floor(Date.now() / 1000),
  });
  switch (result.status) {
    case "ok": {
      const response = NextResponse.redirect(new URL("/", request.url), 303);
      response.headers.append("Set-Cookie", `${COOKIE_NAME}=${result.token}; ${cookieAttributes(ABSOLUTE_LIFETIME_S)}`);
      return response;
    }
    case "throttled": {
      const response = back(request, "?error=wait");
      response.headers.set("Retry-After", String(result.retryAfter));
      return response;
    }
    case "disabled":
      return back(request, "?error=disabled");
    case "denied":
      return back(request, "?error=denied");
  }
}
