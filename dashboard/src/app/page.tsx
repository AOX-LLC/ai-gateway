import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { LiveOverview } from "@/components/LiveOverview";
import { Shell } from "@/components/Shell";
import { config } from "@/lib/config";
import { AuthError } from "@/lib/auth/authed";
import { requireSession } from "@/lib/auth/server";
import { getOverview } from "@/lib/data/overview";
import { parseRange } from "@/lib/data/ranges";

export const dynamic = "force-dynamic";

export default async function Page({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const range = parseRange(typeof params.range === "string" ? params.range : undefined);
  let session;
  try {
    session = await requireSession();
  } catch (error) {
    if (error instanceof AuthError) redirect("/signin");
    throw error;
  }
  const overview = await getOverview(session, range);
  const theme = (await cookies()).get("aig_theme")?.value === "light" ? "light" : "dark";
  return (
    <Shell range={range} theme={theme} sampleData={config().sampleData}>
      <LiveOverview key={range} initial={overview} range={range} />
    </Shell>
  );
}
