import { NextResponse } from "next/server";
import { AuthError } from "@/lib/auth/authed";
import { requireSession } from "@/lib/auth/server";
import { getDecisions } from "@/lib/data";
import { parseRange } from "@/lib/data/ranges";

export const dynamic = "force-dynamic";

/** One page of recent decisions, older than the cursor. */
export async function GET(request: Request): Promise<NextResponse> {
  try {
    const session = await requireSession();
    const params = new URL(request.url).searchParams;
    const page = await getDecisions(session, { range: parseRange(params.get("range")), cursor: params.get("cursor") });
    return NextResponse.json(page);
  } catch (error) {
    if (error instanceof AuthError) return NextResponse.json({ error: "not signed in" }, { status: 401 });
    console.error(`dashboard: decisions could not be read: ${error instanceof Error ? error.name : "error"}`);
    return NextResponse.json({ error: "The database did not answer." }, { status: 503 });
  }
}
