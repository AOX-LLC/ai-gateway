import { NextResponse } from "next/server";
import { AuthError } from "@/lib/auth/authed";
import { requireSession } from "@/lib/auth/server";
import { parseRange } from "@/lib/data/ranges";
import { getOverview } from "@/lib/data/overview";

export const dynamic = "force-dynamic";

/** The three panels' current data, for the page's 15-second refresh. */
export async function GET(request: Request): Promise<NextResponse> {
  try {
    const session = await requireSession();
    const range = parseRange(new URL(request.url).searchParams.get("range"));
    return NextResponse.json(await getOverview(session, range));
  } catch (error) {
    if (error instanceof AuthError) return NextResponse.json({ error: "not signed in" }, { status: 401 });
    throw error;
  }
}
