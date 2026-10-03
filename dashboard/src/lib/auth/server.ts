import "server-only";
import { cookies } from "next/headers";
import { config } from "../config";
import { type AuthedSession, AuthError, mint } from "./authed";
import { COOKIE_NAME, verify } from "./session";

const now = () => Math.floor(Date.now() / 1000);

/** The signed-in session of this request, or undefined. */
export async function currentSession(): Promise<AuthedSession | undefined> {
  const store = await cookies();
  const payload = verify(store.get(COOKIE_NAME)?.value, config().sessionSecret, now());
  return payload ? mint(payload) : undefined;
}

/** The signed-in session of this request; throws AuthError when there is none. */
export async function requireSession(): Promise<AuthedSession> {
  const session = await currentSession();
  if (!session) throw new AuthError();
  return session;
}
