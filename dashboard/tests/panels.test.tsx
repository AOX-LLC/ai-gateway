import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ApprovalsPanel } from "@/components/ApprovalsPanel";
import { DecisionsPanel, type DecisionsView, look } from "@/components/DecisionsPanel";
import { KpiTiles } from "@/components/KpiTiles";
import { NotActive } from "@/components/States";
import type { Approval, Decision, Kpis } from "@/lib/data/types";

const kpis: Kpis = { windowSeconds: 86_400, requests: 120, perMinute: 0.083, p95Ms: 41.5, forwarded: 100, succeeded: 98, successRate: 0.98, blocked: 20, wouldBlock: 3, authFailures: 7 };

const decision = (changes: Partial<Decision> = {}): Decision => ({
  requestId: "00000000-0000-4000-8000-000000000001",
  ts: "2026-10-03T10:00:01.000123Z",
  clientName: "harborline-ops-bot",
  tool: "tickets__assign",
  namespace: "tickets",
  effect: "write",
  outcome: "forwarded",
  blockedBy: null,
  denyCode: null,
  upstreamStatus: "ok",
  durationMs: 12.5,
  wouldBlock: [],
  ...changes,
});

const view = (changes: Partial<DecisionsView> = {}): DecisionsView => ({ page: { decisions: [decision()], nextCursor: null }, error: null, stale: false, since: null, loading: false, index: 0, pagingError: null, ...changes });
const noop = () => undefined;
const html = (element: React.ReactElement) => renderToStaticMarkup(element);

describe("how a decision looks", () => {
  it("gives each outcome a word and an icon as well as a colour", () => {
    expect(look(decision())).toMatchObject({ rail: "success", label: "Forwarded", icon: "check" });
    expect(look(decision({ upstreamStatus: "error" }))).toMatchObject({ rail: "warning", label: "Upstream error", icon: "alert" });
    expect(look(decision({ outcome: "blocked", blockedBy: "scope", denyCode: "scope_denied" }))).toMatchObject({ rail: "danger", label: "Blocked", icon: "ban" });
    expect(look(decision({ outcome: "blocked", blockedBy: "approval", denyCode: "approval_pending" }))).toMatchObject({ rail: "pending", label: "Awaiting approval", icon: "clock" });
  });
});

describe("the KPI tiles", () => {
  it("show the numbers with their units and say what they are over", () => {
    const markup = html(<KpiTiles kpis={kpis} error={null} stale={false} since={null} loading={false} />);

    expect(markup).toContain("120");
    expect(markup).toContain("42");
    expect(markup).toContain("ms");
    expect(markup).toContain("98.0");
    expect(markup).toContain("3 would block");
    expect(markup).toContain("24 hours");
  });

  it("show a dash rather than a made-up number when there is nothing to take a rate or percentile of", () => {
    const markup = html(<KpiTiles kpis={{ ...kpis, requests: 0, p95Ms: null, forwarded: 0, succeeded: 0, successRate: null }} error={null} stale={false} since={null} loading={false} />);

    expect(markup).toContain("No tool calls in the last 24 hours");
    expect(markup).toContain("Nothing was forwarded");
    expect(markup.match(/—/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it("show skeletons while loading, an error with no data, and the last good data marked stale", () => {
    expect(html(<KpiTiles kpis={null} error={null} stale={false} since={null} loading />)).toContain('aria-busy="true"');
    expect(html(<KpiTiles kpis={null} error="The database did not answer." stale={false} since={null} loading={false} />)).toContain("Could not read this panel");
    const stale = html(<KpiTiles kpis={kpis} error="The database did not answer." stale since="10:00:05" loading={false} />);
    expect(stale).toContain("Not updated since 10:00:05 UTC");
    expect(stale).toContain("120");
  });
});

describe("the recent decisions panel", () => {
  it("lists a call with its outcome in words, its layer and its time taken", () => {
    const markup = html(<DecisionsPanel view={view({ page: { decisions: [decision({ outcome: "blocked", blockedBy: "rate_limit", denyCode: "rate_limited", wouldBlock: ["allowlist"] })], nextCursor: "c" } })} onOlder={noop} onNewer={noop} rangeLabel="24 hours" />);

    expect(markup).toContain("Blocked");
    expect(markup).toContain("Rate limit");
    expect(markup).toContain("Would block: Allowlist");
    expect(markup).toContain("13 ms");
    expect(markup).toContain("10:00:01");
  });

  it("is empty with an invitation, errored with a message, and loading with a skeleton", () => {
    expect(html(<DecisionsPanel view={view({ page: { decisions: [], nextCursor: null } })} onOlder={noop} onNewer={noop} rangeLabel="1 hours" />)).toContain("No tool calls in this window");
    expect(html(<DecisionsPanel view={view({ page: null, error: "The database did not answer." })} onOlder={noop} onNewer={noop} rangeLabel="24 hours" />)).toContain("Could not read this panel");
    expect(html(<DecisionsPanel view={view({ loading: true })} onOlder={noop} onNewer={noop} rangeLabel="24 hours" />)).toContain('aria-busy="true"');
  });

  it("says when a page could not be read, keeps the last good page, and disables both buttons while one loads", () => {
    const failed = html(<DecisionsPanel view={view({ pagingError: "The page could not be read." })} onOlder={noop} onNewer={noop} rangeLabel="24 hours" />);
    expect(failed).toContain("The page could not be read. Try again.");
    expect(failed).toContain("tickets__assign");

    const loading = html(<DecisionsPanel view={view({ index: 1, loading: true, page: { decisions: [decision()], nextCursor: "c" } })} onOlder={noop} onNewer={noop} rangeLabel="24 hours" />);
    expect(loading).toContain('aria-busy="true"');
    expect(html(<DecisionsPanel view={view({ index: 1, page: null, error: "x" })} onOlder={noop} onNewer={noop} rangeLabel="24 hours" />)).not.toContain("tries again every 15 seconds");
  });

  it("disables Newer on the first page and Older on the last", () => {
    const markup = html(<DecisionsPanel view={view()} onOlder={noop} onNewer={noop} rangeLabel="24 hours" />);

    expect(markup.match(/disabled=""/g)).toHaveLength(2);
    const middle = html(<DecisionsPanel view={view({ index: 1, page: { decisions: [decision()], nextCursor: "c" } })} onOlder={noop} onNewer={noop} rangeLabel="24 hours" />);
    expect(middle.match(/disabled=""/g)).toBeNull();
    expect(middle).toContain("page 2");
  });
});

const approval = (changes: Partial<Approval> = {}): Approval => ({ id: "a", tool: "tickets__assign", namespace: "tickets", clientName: "harborline-support-bot", status: "pending", createdAt: "2026-10-03T10:00:00.000000Z", expiresAt: "2026-10-03T10:30:00.000000Z", resolvedAt: null, decidedBy: null, ...changes });

describe("the approval queue", () => {
  it("names the upstream, the client and the time left, with Pending as a word and says it is read-only", () => {
    const markup = html(<ApprovalsPanel approvals={{ pending: [approval()], recent: [] }} error={null} stale={false} since={null} loading={false} />);

    expect(markup).toContain("Pending");
    expect(markup).toContain("1 waiting");
    expect(markup).toContain("tickets");
    expect(markup).toContain("harborline-support-bot");
    expect(markup).toContain("expires 10:30:00 UTC");
    expect(markup).toContain("gateway-approver");
    expect(markup).not.toMatch(/<button/);
  });

  it("is a cleared queue with a reason, lists who decided what, and never offers to approve", () => {
    const decided = [approval({ id: "b", status: "approved", resolvedAt: "2026-10-03T10:01:00.000000Z", decidedBy: "Dana Kerr" }), approval({ id: "c", status: "rejected" }), approval({ id: "d", status: "expired" }), approval({ id: "e", status: "consumed" })];
    const markup = html(<ApprovalsPanel approvals={{ pending: [], recent: decided }} error={null} stale={false} since={null} loading={false} />);

    expect(markup).toContain("Queue clear");
    expect(markup).toContain("Every decision is in the audit log.");
    expect(markup).toContain("decided by Dana Kerr");
    for (const word of ["Approved", "Rejected", "Expired", "Approved and used"]) expect(markup).toContain(word);
    expect(markup).not.toMatch(/<button/);
  });

  it("has loading and error states", () => {
    expect(html(<ApprovalsPanel approvals={null} error={null} stale={false} since={null} loading />)).toContain('aria-busy="true"');
    expect(html(<ApprovalsPanel approvals={null} error="The database did not answer." stale={false} since={null} loading={false} />)).toContain("Could not read this panel");
  });
});

describe("the not-active state", () => {
  it("says what is missing and when it starts, in words", () => {
    const markup = html(<NotActive title="Tokens and cost: not active yet">Starts with the classifier.</NotActive>);

    expect(markup).toContain("not active yet");
    expect(markup).toContain('role="status"');
  });
});
