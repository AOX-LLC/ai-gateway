import type { PoolClient } from "pg";
import { afterEach, describe, expect, it } from "vitest";
import { AuthError, mint } from "@/lib/auth/authed";
import { STATEMENT_TIMEOUT_MS, readOnly, setPool } from "@/lib/db";

const session = mint({ sid: "s", iat: 0, seen: 0 });

function fakePool(failOn?: string) {
  const sent: string[] = [];
  let released = 0;
  const client = {
    query: async (text: string) => {
      sent.push(text);
      if (failOn && text.includes(failOn)) throw new Error("boom");
      return { rows: [] };
    },
    release: () => {
      released += 1;
    },
  } as unknown as PoolClient;
  return { pool: { connect: async () => client }, sent, released: () => released };
}

afterEach(() => setPool(undefined));

describe("the read-only runner", () => {
  it("runs the work between a READ ONLY begin with its own timeout and a commit, then gives the connection back", async () => {
    const fake = fakePool();
    setPool(fake.pool as never);

    await readOnly(session, async (client) => client.query("SELECT 1"));

    expect(fake.sent).toEqual([
      "BEGIN READ ONLY",
      `SET LOCAL statement_timeout = ${STATEMENT_TIMEOUT_MS}`,
      "SELECT 1",
      "COMMIT",
    ]);
    expect(fake.released()).toBe(1);
  });

  it("rolls back and releases when the work fails, and rethrows", async () => {
    const fake = fakePool("SELECT 2");
    setPool(fake.pool as never);

    await expect(readOnly(session, async (client) => client.query("SELECT 2"))).rejects.toThrow("boom");

    expect(fake.sent.at(-1)).toBe("ROLLBACK");
    expect(fake.sent).not.toContain("COMMIT");
    expect(fake.released()).toBe(1);
  });

  it("refuses a caller with no session before it takes a connection", async () => {
    let connected = false;
    setPool({ connect: async () => ((connected = true), {} as PoolClient) } as never);

    await expect(readOnly(undefined as never, async () => 1)).rejects.toBeInstanceOf(AuthError);
    await expect(readOnly({ sid: "forged" } as never, async () => 1)).rejects.toBeInstanceOf(AuthError);

    expect(connected).toBe(false);
  });
});
