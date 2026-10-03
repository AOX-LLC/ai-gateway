import { issue } from "./session";
import type { Throttle } from "./throttle";
import { verifyPassword } from "./password";

export type SignInResult =
  | { status: "ok"; token: string }
  | { status: "denied" }
  | { status: "throttled"; retryAfter: number }
  | { status: "disabled" };

export type SignInDeps = {
  hash: string;
  secret: string;
  throttle: Throttle;
  now: () => number;
  verify?: (password: string, hash: string) => Promise<boolean>;
};

const MAX_PASSWORD_LENGTH = 1024;

/** Check a password. While throttled the password is not looked at, and with no credential set
 * nobody gets in. Every failure is the same `denied`: it never says whether anything was close. */
export async function attemptSignIn(password: string, deps: SignInDeps): Promise<SignInResult> {
  if (!deps.hash) return { status: "disabled" };
  const wait = deps.throttle.retryAfter(deps.now());
  if (wait > 0) return { status: "throttled", retryAfter: wait };
  const matches =
    password.length > 0 && password.length <= MAX_PASSWORD_LENGTH && (await (deps.verify ?? verifyPassword)(password, deps.hash));
  if (!matches) {
    deps.throttle.failed(deps.now());
    return { status: "denied" };
  }
  deps.throttle.succeeded();
  return { status: "ok", token: issue(deps.secret, deps.now()) };
}
