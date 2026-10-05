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
import { chmodSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright";
import type { Browser, Page } from "playwright";
import { HEIGHT, REPO_DIR, WIDTH, parseTheme, themeDir } from "./lib.ts";
import type { Scene, Theme } from "./lib.ts";

const BASE_URL = process.env.DASHBOARD_URL ?? "http://127.0.0.1:4400";
const PASSWORD_FILE = join(REPO_DIR, ".demo", "password");
const VIEWPORT = { width: WIDTH, height: HEIGHT };

/** How the clip moves: a scroll takes SCROLL_MS and stops BELOW_TOPBAR_PX under the pinned topbar. */
const SCROLL_MS = 1400;
const SETTLE_MS = 600;
const BELOW_TOPBAR_PX = 72;

/** How long each panel is held, in recorded time. The traffic simulator runs under all of them. */
const LIVE_HOLD_MS = 20_000;
const LAYERS_HOLD_MS = 6_000;
const DECISIONS_HOLD_MS = 14_000;
const APPROVALS_HOLD_MS = 6_000;

/** Above the page content, which has its own sticky table headers (z-index 1). */
const TOPBAR_STACKING = "20";

const HOSTNAME = /127\.0\.0\.1|localhost|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|tailscale|\.ts\.net|\.local\b/;

const themeName: Theme = parseTheme(process.env.THEME ?? "dark");
const outDir = themeDir(themeName);
const scenes: Scene[] = [];
const consoleErrors: string[] = [];
let videoStart = 0;
const now = () => Date.now() - videoStart;
/** A scene starts when its page has settled, not when navigation began: settle() moves the mark, so the
 * edit never shows a half-drawn page. */
let sceneStart = 0;

function demoPassword(): string {
  const password = readFileSync(PASSWORD_FILE, "utf8").trim();
  if (password === "") throw new Error(`${PASSWORD_FILE} is empty: run scripts/run_dashboard_demo.sh up first`);
  return password;
}

async function signInOffCamera(browser: Browser): Promise<string> {
  const context = await browser.newContext({ viewport: VIEWPORT, colorScheme: themeName });
  const page = await context.newPage();
  await page.goto(`${BASE_URL}/signin`);
  await page.fill("#password", demoPassword());
  await page.click("button[type=submit]");
  await page.waitForURL(`${BASE_URL}/`);
  if (themeName === "light") await page.getByRole("button", { name: "Switch to light theme" }).click();
  const statePath = join(outDir, "state.json");
  await context.storageState({ path: statePath });
  chmodSync(statePath, 0o600); // it holds a session cookie
  await context.close();
  return statePath;
}

/** The topbar and the sidebar scroll away in the product; a clip needs the topbar (and the "Sample data"
 * pill in it) in every frame, and the sidebar keeps the left of the frame from going blank. So the
 * recording page pins both. A recording aid only: the dashboard is unchanged, and its strict CSP stays on
 * (setting a property from script is allowed; an injected stylesheet is not). */
async function pinChrome(page: Page): Promise<void> {
  await page.evaluate((stacking) => {
    const topbar = document.querySelector<HTMLElement>(".pui-topbar");
    const sidebar = document.querySelector<HTMLElement>(".pui-sidebar");
    if (!topbar || !sidebar) throw new Error("the page has no topbar or no sidebar");
    Object.assign(topbar.style, { position: "sticky", top: "0", zIndex: stacking });
    Object.assign(sidebar.style, { position: "sticky", top: "0", alignSelf: "flex-start", height: "100vh", overflowY: "hidden" });
  }, TOPBAR_STACKING);
}

async function settle(page: Page): Promise<void> {
  await page.waitForLoadState("networkidle");
  const states = await page.evaluate(() =>
    document.fonts.ready.then(() => [...document.fonts].map((face) => face.status)),
  );
  if (!states.includes("loaded") || states.includes("error")) throw new Error("the fonts did not load");
  await pinChrome(page);
  await page.waitForTimeout(SETTLE_MS);
  sceneStart = now();
}

async function assertWhatIsOnScreen(page: Page, sceneId: string): Promise<void> {
  const pill = page.locator(".pui-sample-pill");
  const box = await pill.boundingBox();
  const onScreen = box !== null && box.y >= 0 && box.y + box.height <= VIEWPORT.height;
  if (!onScreen) throw new Error(`scene ${sceneId}: the "Sample data" pill is not on screen`);
  const text = await page.innerText("body");
  if (!text.includes("Harborline Supply Co. (fictional)")) throw new Error(`scene ${sceneId}: the company is not labelled fictional`);
  if (HOSTNAME.test(text)) throw new Error(`scene ${sceneId}: a hostname or address is in the page text`);
}

/** Run `body` as one scene and record when it started and ended on the video clock. */
async function scene(
  page: Page,
  meta: Pick<Scene, "id" | "caption"> & Partial<Pick<Scene, "speed" | "gif">>,
  body: () => Promise<void>,
): Promise<void> {
  sceneStart = now();
  await body();
  await assertWhatIsOnScreen(page, meta.id);
  scenes.push({ speed: 1, gif: false, ...meta, startMs: sceneStart, endMs: now() });
}

/** Scroll so `selector` sits just under the topbar, easing in and out so the motion reads as a hand. */
async function scrollTo(page: Page, selector: string): Promise<void> {
  await page.evaluate(
    ([target, duration, offset]) =>
      new Promise<void>((resolve) => {
        const element = document.querySelector(target as string);
        if (!element) throw new Error(`no element matches ${target}`);
        const from = window.scrollY;
        const to = from + element.getBoundingClientRect().top - (offset as number);
        const began = performance.now();
        const ease = (t: number) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
        const step = (time: number) => {
          const t = Math.min(1, (time - began) / (duration as number));
          window.scrollTo(0, from + (to - from) * ease(t));
          if (t < 1) requestAnimationFrame(step);
          else resolve();
        };
        requestAnimationFrame(step);
      }),
    [selector, SCROLL_MS, BELOW_TOPBAR_PX],
  );
}

async function recordScenes(page: Page): Promise<void> {
  await scene(
    page,
    { id: "volume-live", gif: true, speed: 4, caption: "Simulated traffic arrives live; the overview refreshes every 15 seconds" },
    async () => {
      await page.goto(`${BASE_URL}/?range=1h`);
      await page.waitForSelector("#volume svg");
      await settle(page);
      await page.waitForTimeout(LIVE_HOLD_MS);
    },
  );
  await scene(page, { id: "to-layers", caption: "" }, () => scrollTo(page, "#layers"));
  await scene(
    page,
    { id: "layers", gif: true, speed: 2, caption: "Blocked calls, by the layer that stopped them" },
    () => page.waitForTimeout(LAYERS_HOLD_MS),
  );
  await scene(page, { id: "to-decisions", caption: "" }, () => scrollTo(page, "#decisions"));
  await scene(
    page,
    { id: "decisions", gif: true, speed: 4, caption: "Every call, with its outcome and the layer that decided it" },
    () => page.waitForTimeout(DECISIONS_HOLD_MS),
  );
  await scene(page, { id: "to-approvals", caption: "" }, () => scrollTo(page, "#approvals"));
  await scene(
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
  videoStart = Date.now();
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
    await recordTake(browser, await signInOffCamera(browser));
  } finally {
    // The session cookie and the scratch video go whether or not the take worked.
    rmSync(join(outDir, "state.json"), { force: true });
    rmSync(join(outDir, "video-tmp"), { recursive: true, force: true });
    await browser.close();
  }
  writeFileSync(
    join(outDir, "timeline.json"),
    JSON.stringify({ project: "ai-gateway", theme: themeName, scenes }, null, 2),
  );
  console.log(`recorded ${scenes.length} scenes -> ${outDir}`);
}

main().catch((error) => {
  console.error(String(error instanceof Error ? error.message : error).split("\n")[0]);
  process.exit(1);
});
