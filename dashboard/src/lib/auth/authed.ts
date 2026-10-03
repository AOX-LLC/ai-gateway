import type { SessionPayload } from "./session";

/** An `AuthedSession` can only come from `mint`, which only the code that has checked the signed
 * cookie calls. Every function that reads data takes one and checks it again with `assertAuthed`,
 * so a data function cannot be reached by a code path that forgot to ask who is signed in. */

declare const authedBrand: unique symbol;
export type AuthedSession = Readonly<{ sid: string; [authedBrand]: true }>;

export class AuthError extends Error {
  constructor() {
    super("not signed in");
  }
}

const minted = new WeakSet<object>();

export function mint(payload: SessionPayload): AuthedSession {
  const session = Object.freeze({ sid: payload.sid }) as AuthedSession;
  minted.add(session);
  return session;
}

export function assertAuthed(session: unknown): asserts session is AuthedSession {
  if (typeof session !== "object" || session === null || !minted.has(session)) throw new AuthError();
}
