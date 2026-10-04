import { NextResponse } from "next/server";

// A 303 whose Location is a path. Inside the container the request's own URL names the address the
// server listens on (0.0.0.0), not the one the browser used, so an absolute URL built from it sends
// the browser somewhere it cannot go. A relative Location is resolved against the address it used.
export function seeOther(path: string): NextResponse {
  // Only a path on this server: "//host" and "/\\host" are read by a browser as another site.
  if (!path.startsWith("/") || path.startsWith("//") || path.startsWith("/\\")) {
    throw new Error("seeOther takes a path on this server");
  }
  return new NextResponse(null, { status: 303, headers: { Location: path } });
}
