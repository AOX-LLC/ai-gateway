import { describe, expect, it } from "vitest";
import { hashPassword, verifyPassword } from "@/lib/auth/password";

describe("the admin credential", () => {
  it("verifies the password it hashed and no other", async () => {
    const hash = await hashPassword("correct horse battery staple");

    expect(await verifyPassword("correct horse battery staple", hash)).toBe(true);
    expect(await verifyPassword("correct horse battery stapl", hash)).toBe(false);
    expect(await verifyPassword("", hash)).toBe(false);
  });

  it("salts each hash and writes it without a dollar sign, which Compose would read as a variable", async () => {
    const [first, second] = [await hashPassword("same"), await hashPassword("same")];

    expect(first).not.toBe(second);
    expect(first).not.toContain("$");
    expect(first.split(":")).toHaveLength(6);
  });

  it.each([
    ["not a hash"],
    [""],
    ["scrypt:32768:8:1:c2FsdA:"],
    ["bcrypt:32768:8:1:c2FsdHNhbHRzYWx0:a2V5a2V5a2V5a2V5a2V5"],
    ["scrypt:32769:8:1:c2FsdHNhbHRzYWx0:a2V5a2V5a2V5a2V5a2V5"], // N not a power of two
    ["scrypt:1073741824:8:1:c2FsdHNhbHRzYWx0:a2V5a2V5a2V5a2V5a2V5"], // would take the process's memory
    ["scrypt:32768:8:1:c2FsdHNhbHRzYWx0:a2V5a2V5a2V5a2V5a2V5:extra"],
  ])("never matches a hash it cannot read: %s", async (encoded) => {
    expect(await verifyPassword("anything", encoded)).toBe(false);
  });

  it("accepts a hash made by scripts/set_dashboard_password.py, which is how the credential is written", async () => {
    // Made in Python with hash_password("correct horse battery", salt=bytes(range(16))).
    const fromPython = "scrypt:32768:8:1:AAECAwQFBgcICQoLDA0ODw:xy6UCICz8Vv_P6JqOkkUD4DOmhxN0tXSe6sQAUBxupQ";

    expect(await verifyPassword("correct horse battery", fromPython)).toBe(true);
    expect(await verifyPassword("correct horse batterz", fromPython)).toBe(false);
  });

  it("treats the same text in another Unicode form as the same password", async () => {
    const hash = await hashPassword("café");

    expect(await verifyPassword("café", hash)).toBe(true);
  });
});
