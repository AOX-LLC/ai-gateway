import { randomBytes, scrypt, timingSafeEqual } from "node:crypto";

/** The admin credential is a scrypt hash in the environment, written as
 *   scrypt:<N>:<r>:<p>:<salt, base64url>:<hash, base64url>
 * (no `$`, which Compose would read as a variable). One verification takes about 32 MiB, so they
 * are done one at a time. */

export const SCRYPT = { N: 32768, r: 8, p: 1, keyLength: 32, saltLength: 16 } as const;
const MAX_MEMORY = 128 * 1024 * 1024;
const PREFIX = "scrypt";

function derive(password: string, salt: Buffer, n: number, r: number, p: number, length: number) {
  return new Promise<Buffer>((resolve, reject) => {
    scrypt(password.normalize("NFKC"), salt, length, { N: n, r, p, maxmem: MAX_MEMORY }, (error, key) =>
      error ? reject(error) : resolve(key),
    );
  });
}

let queue: Promise<unknown> = Promise.resolve();

/** Run `work` after every earlier call has finished, so verifications never overlap. */
function oneAtATime<T>(work: () => Promise<T>): Promise<T> {
  const next = queue.then(work, work);
  queue = next.catch(() => undefined);
  return next;
}

export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(SCRYPT.saltLength);
  const key = await oneAtATime(() => derive(password, salt, SCRYPT.N, SCRYPT.r, SCRYPT.p, SCRYPT.keyLength));
  const parts = [PREFIX, SCRYPT.N, SCRYPT.r, SCRYPT.p, salt.toString("base64url"), key.toString("base64url")];
  return parts.join(":");
}

type Parsed = { n: number; r: number; p: number; salt: Buffer; key: Buffer };

function parse(encoded: string): Parsed | undefined {
  const [prefix, n, r, p, salt, key, extra] = encoded.split(":");
  if (prefix !== PREFIX || extra !== undefined || !salt || !key) return undefined;
  const numbers = [n, r, p].map((part) => Number(part));
  if (numbers.some((value) => !Number.isInteger(value) || value < 1)) return undefined;
  const [nn, rr, pp] = numbers as [number, number, number];
  // Refuse parameters that would make a verification take the process's memory.
  if (nn > 2 ** 17 || rr > 16 || pp > 4 || (nn & (nn - 1)) !== 0) return undefined;
  const saltBytes = Buffer.from(salt, "base64url");
  const keyBytes = Buffer.from(key, "base64url");
  if (saltBytes.length < 8 || keyBytes.length < 16) return undefined;
  return { n: nn, r: rr, p: pp, salt: saltBytes, key: keyBytes };
}

/** Whether `password` is the one `encoded` hashes. A hash that cannot be read is never a match. */
export async function verifyPassword(password: string, encoded: string): Promise<boolean> {
  const parsed = parse(encoded);
  if (!parsed) return false;
  const key = await oneAtATime(() => derive(password, parsed.salt, parsed.n, parsed.r, parsed.p, parsed.key.length));
  return timingSafeEqual(key, parsed.key);
}
