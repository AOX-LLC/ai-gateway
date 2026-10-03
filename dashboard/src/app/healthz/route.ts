/** Answers while the process is serving. It reads nothing and needs no sign-in: a health check that
 * touched the database would take the dashboard down with it. */
export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json({ status: "ok" }, { headers: { "Cache-Control": "no-store" } });
}
