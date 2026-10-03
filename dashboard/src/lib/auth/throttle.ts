/** Failed sign-ins, counted for the whole dashboard. Behind Docker's port publishing every client
 * arrives from the bridge's address, so a per-address count would be one count for everybody anyway;
 * an admin tool on 127.0.0.1 accepts that someone who can reach it can also lock it for a minute.
 * After MAX_FAILURES failures inside WINDOW_S, sign-in is refused for LOCKOUT_S without the password
 * being looked at; a success clears the count.
 *
 * An attempt is counted when it starts (`begin`), not when it fails: checking the password takes
 * time, and a burst of guesses sent at once would otherwise all pass the check before any of them
 * had failed. A success takes its own attempt back (`succeeded` clears the count). */

export const MAX_FAILURES = 5;
export const WINDOW_S = 15 * 60;
export const LOCKOUT_S = 60;

export class Throttle {
  private failures: number[] = [];
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
    this.failures = [...this.failures.filter((at) => now - at < WINDOW_S), now];
    if (this.failures.length >= MAX_FAILURES) {
      this.lockedUntil = now + LOCKOUT_S;
      this.failures = [];
    }
  }

  succeeded(): void {
    this.failures = [];
    this.lockedUntil = 0;
  }
}

export const signInThrottle = new Throttle();
