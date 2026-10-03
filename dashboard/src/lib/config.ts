/** The dashboard's settings, read from the environment once and checked. A missing or weak one stops
 * the process from serving: nothing here falls back to a default that would open the dashboard. */

export type Config = {
  readonly databaseUrl: string;
  /** Empty: nobody can sign in, and the sign-in page says so. */
  readonly adminPasswordHash: string;
  readonly sessionSecret: string;
  readonly sampleData: boolean;
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
  };
}

let cached: Config | undefined;

/** The settings of this process. Throws ConfigError on the first call if they are wrong. */
export function config(): Config {
  cached ??= parseConfig(process.env);
  return cached;
}
