import { NextResponse } from "next/server";
import { isSameOrigin } from "@/lib/auth/origin";
import { COOKIE_NAME, cookieAttributes } from "@/lib/auth/session";

export const dynamic = "force-dynamic";

export function POST(request: Request): NextResponse {
  if (!isSameOrigin(request.headers)) return new NextResponse("Forbidden", { status: 403 });
  const response = NextResponse.redirect(new URL("/signin", request.url), 303);
  response.headers.append("Set-Cookie", `${COOKIE_NAME}=; ${cookieAttributes(0)}`);
  return response;
}
