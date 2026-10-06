// Records the signed-in demo dashboard under live simulated traffic: one video per theme, plus a
// timeline of scenes (what is on screen, when, and whether it is a wait that edit.ts may speed up).
//
//   THEME=light|dark node record.ts
//
// For the demo stack only (scripts/run_dashboard_demo.sh, which record.sh drives). The demo-only admin
// password is read from the git-ignored .demo/password and typed into the sign-in form in a throwaway
// browser context before recording starts, so no password is ever on camera and the recorded context
// starts with a session cookie only. Nothing here reads .env or the real admin password.
//
// It checks what it is filming and fails rather than record the wrong thing: the "Sample data" pill is
// on screen at the end of every scene, no hostname or address is in the page text, the fonts loaded and
// the browser's console shows no error. (OCR of the finished frames is a second check: check-frames.ts.)
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright";
import type { Browser, Page } from "playwright";
import { BASE_URL, Timeline, VIEWPORT, scrollTo, signInOffCamera } from "./dashboard.ts";
import { parseTheme, themeDir } from "./lib.ts";
import type { Theme } from "./lib.ts";

/** How long each panel is held, in recorded time. The traffic simulator runs under all of them. */
const LIVE_HOLD_MS = 20_000;
const LAYERS_HOLD_MS = 6_000;
const DECISIONS_HOLD_MS = 14_000;
const APPROVALS_HOLD_MS = 6_000;

const themeName: Theme = parseTheme(process.env.THEME ?? "dark");
const outDir = themeDir(themeName);
const timeline = new Timeline();
const consoleErrors: string[] = [];

async function recordScenes(page: Page): Promise<void> {
  await timeline.scene(
    page,
    { id: "volume-live", gif: true, speed: 4, caption: "Simulated traffic arrives live; the overview refreshes every 15 seconds" },
    async () => {
      await page.goto(`${BASE_URL}/?range=1h`);
      await page.waitForSelector("#volume svg");
      await timeline.settle(page);
      await page.waitForTimeout(LIVE_HOLD_MS);
    },
  );
  await timeline.scene(page, { id: "to-layers", caption: "" }, () => scrollTo(page, "#layers"));
  await timeline.scene(
    page,
    { id: "layers", gif: true, speed: 2, caption: "Blocked calls, by the layer that stopped them" },
    () => page.waitForTimeout(LAYERS_HOLD_MS),
  );
  await timeline.scene(page, { id: "to-decisions", caption: "" }, () => scrollTo(page, "#decisions"));
  await timeline.scene(
    page,
    { id: "decisions", gif: true, speed: 4, caption: "Every call, with its outcome and the layer that decided it" },
    () => page.waitForTimeout(DECISIONS_HOLD_MS),
  );
  await timeline.scene(page, { id: "to-approvals", caption: "" }, () => scrollTo(page, "#approvals"));
  await timeline.scene(
    page,
    { id: "approvals", gif: true, speed: 2, caption: "Writes wait for a person; the dashboard cannot approve" },
    () => page.waitForTimeout(APPROVALS_HOLD_MS),
  );
}

async function recordTake(browser: Browser, statePath: string): Promise<void> {
  const context = await browser.newContext({
    viewport: VIEWPORT,
    colorScheme: themeName,
    locale: "en-US",
    timezoneId: "UTC",
    reducedMotion: "reduce",
    storageState: statePath,
    recordVideo: { dir: join(outDir, "video-tmp"), size: VIEWPORT },
  });
  const page = await context.newPage();
  page.on("console", (message) => message.type() === "error" && consoleErrors.push(message.text().slice(0, 200)));
  page.on("pageerror", (error) => consoleErrors.push(String(error).slice(0, 200)));
  timeline.startClock();
  await recordScenes(page);
  const theme = await page.locator("html").getAttribute("data-theme");
  if (theme !== themeName) throw new Error(`the page is in the ${theme} theme, not ${themeName}`);
  if (consoleErrors.length > 0) throw new Error(`the browser console shows errors: ${consoleErrors.slice(0, 3).join("; ")}`);
  const video = page.video();
  await context.close();
  await video?.saveAs(join(outDir, "raw.webm"));
}

async function main(): Promise<void> {
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch();
  try {
    await recordTake(browser, await signInOffCamera(browser, themeName, outDir));
  } finally {
    // The session cookie and the scratch video go whether or not the take worked.
    rmSync(join(outDir, "state.json"), { force: true });
    rmSync(join(outDir, "video-tmp"), { recursive: true, force: true });
    await browser.close();
  }
  writeFileSync(
    join(outDir, "timeline.json"),
    JSON.stringify({ project: "ai-gateway", theme: themeName, scenes: timeline.scenes }, null, 2),
  );
  console.log(`recorded ${timeline.scenes.length} scenes -> ${outDir}`);
}

main().catch((error) => {
  console.error(String(error instanceof Error ? error.message : error).split("\n")[0]);
  process.exit(1);
});
