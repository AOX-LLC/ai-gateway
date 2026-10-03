"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { clock, windowLabel } from "@/lib/format";
import type { Approvals, DecisionsPage, Kpis, Overview, Result } from "@/lib/data/types";
import { ApprovalsPanel } from "./ApprovalsPanel";
import { type DecisionsView, DecisionsPanel } from "./DecisionsPanel";
import { Icon } from "./Icon";
import { KpiTiles } from "./KpiTiles";
import { NotActive } from "./States";

/** The overview as it refreshes: every 15 seconds while the tab is visible, straight away when it
 * becomes visible again after a gap, and never while it is hidden. A refresh that fails keeps the
 * last good data on screen and says how old it is. */

export const POLL_MS = 15_000;
const RANGE_SECONDS = { "1h": 3600, "24h": 86_400, "7d": 604_800, "30d": 2_592_000 } as const;

type Panel<T> = { data: T | null; error: string | null; stale: boolean; since: string | null };

function start<T>(result: Result<T>, at: string): Panel<T> {
  return result.ok
    ? { data: result.data, error: null, stale: false, since: at }
    : { data: null, error: result.error, stale: false, since: null };
}

function next<T>(previous: Panel<T>, result: Result<T>, at: string): Panel<T> {
  if (result.ok) return { data: result.data, error: null, stale: false, since: at };
  return { data: previous.data, error: result.error, stale: previous.data !== null, since: previous.since };
}

export function LiveOverview({ initial, range }: { initial: Overview; range: keyof typeof RANGE_SECONDS }) {
  const [kpis, setKpis] = useState<Panel<Kpis>>(() => start(initial.kpis, initial.at));
  const [approvals, setApprovals] = useState<Panel<Approvals>>(() => start(initial.approvals, initial.at));
  const [live, setLive] = useState<Panel<DecisionsPage>>(() => start(initial.decisions, initial.at));
  const [updatedAt, setUpdatedAt] = useState(initial.at);
  // Older pages are fetched on request; page 0 is the live one that the refresh keeps current.
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [older, setOlder] = useState<{ page: DecisionsPage | null; loading: boolean; error: string | null }>({
    page: null,
    loading: false,
    error: null,
  });
  const index = cursors.length - 1;
  const router = useRouter();
  const indexRef = useRef(0);
  const lastRun = useRef(0);
  useEffect(() => {
    indexRef.current = index;
  }, [index]);

  const refresh = useCallback(async () => {
    lastRun.current = Date.now();
    try {
      const response = await fetch(`/api/live?range=${range}`, { cache: "no-store" });
      if (response.status === 401) {
        router.replace("/signin");
        return;
      }
      if (!response.ok) throw new Error(String(response.status));
      const overview = (await response.json()) as Overview;
      setKpis((p) => next(p, overview.kpis, overview.at));
      setApprovals((p) => next(p, overview.approvals, overview.at));
      setLive((p) => (indexRef.current === 0 ? next(p, overview.decisions, overview.at) : p));
      setUpdatedAt(overview.at);
    } catch {
      const message = "The dashboard could not reach its server.";
      const failed = <T,>(p: Panel<T>): Panel<T> => ({ ...p, error: message, stale: p.data !== null });
      setKpis(failed);
      setApprovals(failed);
      setLive(failed);
    }
  }, [range, router]);

  useEffect(() => {
    lastRun.current = Date.now();
    let timer: ReturnType<typeof setInterval> | undefined;
    const run = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const arm = () => {
      clearInterval(timer);
      timer = setInterval(run, POLL_MS);
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        clearInterval(timer);
        timer = undefined;
        return;
      }
      if (Date.now() - lastRun.current >= POLL_MS) void refresh();
      arm();
    };
    if (document.visibilityState === "visible") arm();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refresh]);

  const loadPage = useCallback(
    async (cursor: string | null) => {
      setOlder((p) => ({ ...p, loading: true, error: null }));
      try {
        const query = new URLSearchParams({ range });
        if (cursor) query.set("cursor", cursor);
        const response = await fetch(`/api/decisions?${query}`, { cache: "no-store" });
        if (response.status === 401) {
          router.replace("/signin");
          return false;
        }
        if (!response.ok) throw new Error(String(response.status));
        setOlder({ page: (await response.json()) as DecisionsPage, loading: false, error: null });
        return true;
      } catch {
        setOlder((p) => ({ ...p, loading: false, error: "The page could not be read." }));
        return false;
      }
    },
    [range, router],
  );

  const goOlder = useCallback(async () => {
    const page = index === 0 ? live.data : older.page;
    if (!page?.nextCursor) return;
    if (await loadPage(page.nextCursor)) setCursors((c) => [...c, page.nextCursor]);
  }, [index, live.data, older.page, loadPage]);

  const goNewer = useCallback(async () => {
    if (index === 0) return;
    if (index === 1) {
      setCursors([null]);
      setOlder({ page: null, loading: false, error: null });
      return;
    }
    if (await loadPage(cursors[index - 1] ?? null)) setCursors((c) => c.slice(0, -1));
  }, [index, cursors, loadPage]);

  const view: DecisionsView =
    index === 0
      ? { page: live.data, error: live.error, stale: live.stale, since: live.since ? clock(live.since) : null, loading: false, index }
      : { page: older.page, error: older.error, stale: false, since: null, loading: older.loading, index };

  return (
    <>
      <div className="pui-summary-line" role="status">
        <span className="pui-muted">
          <Icon name="refresh" size="sm" /> Updated {clock(updatedAt)} UTC · refreshes every 15 seconds while this tab is open
        </span>
      </div>
      <KpiTiles
        kpis={kpis.data}
        error={kpis.error}
        stale={kpis.stale}
        since={kpis.since ? clock(kpis.since) : null}
        loading={false}
      />
      <div className="panel-grid">
        <DecisionsPanel view={view} onOlder={goOlder} onNewer={goNewer} rangeLabel={windowLabel(RANGE_SECONDS[range])} />
        <ApprovalsPanel
          approvals={approvals.data}
          error={approvals.error}
          stale={approvals.stale}
          since={approvals.since ? clock(approvals.since) : null}
          loading={false}
        />
      </div>
      <section aria-label="Tokens and cost">
        <NotActive title="Tokens and cost: not active yet">
          Token counts and model cost start with the injection classifier in Phase 4. Until a model is called there is nothing to count.
        </NotActive>
      </section>
    </>
  );
}
