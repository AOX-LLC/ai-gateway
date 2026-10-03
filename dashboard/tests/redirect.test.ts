import { describe, expect, it } from "vitest";
import { seeOther } from "@/lib/redirect";

describe("seeOther", () => {
  it("answers 303 with a path, never an absolute URL naming the listen address", () => {
    const response = seeOther("/signin?error=wait");
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/signin?error=wait");
    expect(response.headers.get("location")).not.toMatch(/^[a-z]+:/i);
  });

  it("refuses anything that is not a path on this server", () => {
    for (const target of ["https://example.com", "//example.com", "/\\example.com", "signin", ""]) {
      expect(() => seeOther(target)).toThrow();
    }
  });
});
