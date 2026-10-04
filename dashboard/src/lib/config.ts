/** The dashboard's settings, read from the environment once and checked. A missing or weak one stops
 * the process from serving: nothing here falls back to a default that would open the dashboard. */

import { parseHosts } from "./auth/hosts";

export type Config = {
  readonly databaseUrl: string;
  /** Empty: nobody can sign in, and the sign-in page says so. */
  readonly adminPasswordHash: string;
  readonly sessionSecret: string;
  readonly sampleData: boolean;
  /** A demo stack: "now" is the newest recorded request, so a seeded state looks the same whenever it is
   * looked at (see lib/data/clock.ts). Never set it against real data. */
  readonly demo: boolean;
  /** The Host values this dashboard answers to (see auth/hosts.ts). */
  readonly allowedHosts: readonly string[];
};

export class ConfigError extends Error {}

const MIN_SECRET_LENGTH = 32;

export function parseConfig(env: Readonly<Record<string, string | undefined>>): Config {
  const databaseUrl = env.DASHBOARD_DATABASE_URL ?? "";
  if (!databaseUrl) throw new ConfigError("DASHBOARD_DATABASE_URL is not set");
  const sessionSecret = env.DASHBOARD_SESSION_SECRET ?? "";
  if (sessionSecret.length < MIN_SECRET_LENGTH || sessionSecret.includes("change-me")) {
    throw new ConfigError(
      `DASHBOARD_SESSION_SECRET must be at least ${MIN_SECRET_LENGTH} characters and not a placeholder`,
    );
  }
  return {
    databaseUrl,
    adminPasswordHash: env.DASHBOARD_ADMIN_PASSWORD_HASH ?? "",
    sessionSecret,
    sampleData: env.DASHBOARD_SAMPLE_DATA === "1",
    demo: env.DASHBOARD_DEMO === "1",
    allowedHosts: parseHosts(env.DASHBOARD_ALLOWED_HOSTS),
  };
}

let cached: Config | undefined;

/** The settings of this process. Throws ConfigError on the first call if they are wrong. */
export function config(): Config {
  cached ??= parseConfig(process.env);
  return cached;
}
