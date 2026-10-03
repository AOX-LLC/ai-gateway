/** Failed sign-ins, counted for the whole dashboard. Behind Docker's port publishing every client
 * arrives from the bridge's address, so a per-address count would be one count for everybody anyway;
 * an admin tool on 127.0.0.1 accepts that someone who can reach it can also lock it.
 *
 * Five attempts are allowed. The fifth starts a lockout of LOCKOUT_S during which the password is not
 * looked at; every attempt made after a lockout is one more, and starts a lockout twice as long as
 * the last, up to MAX_LOCKOUT_S. So a guesser gets five tries and then one per lockout (about one
 * every 15 minutes, 96 a day). The count does not expire with time, because a window that forgets
 * old failures gives the guesser a fresh burst once the lockouts outgrow it: only a success clears it
 * (or a restart, which a guesser cannot cause). The real admin who is locked out waits at most 15
 * minutes for an attempt that is looked at.
 *
 * An attempt is counted when it starts (`begin`), not when it fails: checking the password takes
 * time, and a burst of guesses sent at once would otherwise all pass the check before any of them
 * had failed. A success takes its own attempt back (`succeeded` clears the count). */

export const MAX_FAILURES = 5;
export const LOCKOUT_S = 60;
export const MAX_LOCKOUT_S = 15 * 60;

export class Throttle {
  private attempts = 0;
  private lockedUntil = 0;

  /** Seconds to wait, or 0 if an attempt may go ahead. */
  retryAfter(now: number): number {
    return now < this.lockedUntil ? Math.ceil(this.lockedUntil - now) : 0;
  }

  /** Ask to make an attempt. Returns the seconds to wait (and counts nothing) when locked; returns 0
   * and counts the attempt as a failure at once when allowed. */
  begin(now: number): number {
    const wait = this.retryAfter(now);
    if (wait === 0) this.failed(now);
    return wait;
  }

  failed(now: number): void {
    this.attempts += 1;
    if (this.attempts < MAX_FAILURES) return;
    const lockouts = this.attempts - MAX_FAILURES;
    this.lockedUntil = now + Math.min(LOCKOUT_S * 2 ** lockouts, MAX_LOCKOUT_S);
  }

  succeeded(): void {
    this.attempts = 0;
    this.lockedUntil = 0;
  }
}

export const signInThrottle = new Throttle();
