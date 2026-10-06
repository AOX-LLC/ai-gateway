// What every dashboard recording shares: the demo-stack sign-in (off camera), the pinned chrome, the
// settle and scroll helpers, the checks that what is on screen is what should be, and the scene timeline.
// Used by record.ts (the live-traffic clip) and real-client/record.ts (the Claude Code clip).
// For the demo stack only (scripts/run_dashboard_demo.sh): the demo-only admin password is read from the
// git-ignored .demo/password and typed into the sign-in form in a throwaway browser context before
// recording starts, so no password is ever on camera. Nothing here reads .env or the real admin password.
import { chmodSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { Browser, Page } from "playwright";
import { HEIGHT, REPO_DIR, WIDTH } from "./lib.ts";
import type { Scene, Theme } from "./lib.ts";

export const BASE_URL = process.env.DASHBOARD_URL ?? "http://127.0.0.1:4400";
export const VIEWPORT = { width: WIDTH, height: HEIGHT };
const PASSWORD_FILE = join(REPO_DIR, ".demo", "password");

/** How a clip moves: a scroll takes SCROLL_MS and stops BELOW_TOPBAR_PX under the pinned topbar. */
const SCROLL_MS = 1400;
const SETTLE_MS = 600;
const BELOW_TOPBAR_PX = 72;

/** Above the page content, which has its own sticky table headers (z-index 1). */
const TOPBAR_STACKING = "20";

export const HOSTNAME =
  /127\.0\.0\.1|localhost|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|tailscale|\.ts\.net|\.local\b/;

function demoPassword(): string {
  const password = readFileSync(PASSWORD_FILE, "utf8").trim();
  if (password === "") throw new Error(`${PASSWORD_FILE} is empty: run scripts/run_dashboard_demo.sh up first`);
  return password;
}

/** Signs in in a throwaway context, in the theme asked for, and returns the path of the saved session. */
export async function signInOffCamera(browser: Browser, theme: Theme, outDir: string): Promise<string> {
  const context = await browser.newContext({ viewport: VIEWPORT, colorScheme: theme });
  const page = await context.newPage();
  await page.goto(`${BASE_URL}/signin`);
  await page.fill("#password", demoPassword());
  await page.click("button[type=submit]");
  await page.waitForURL(`${BASE_URL}/`);
  if (theme === "light") await page.getByRole("button", { name: "Switch to light theme" }).click();
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
export async function pinChrome(page: Page): Promise<void> {
  await page.evaluate((stacking) => {
    const topbar = document.querySelector<HTMLElement>(".pui-topbar");
    const sidebar = document.querySelector<HTMLElement>(".pui-sidebar");
    if (!topbar || !sidebar) throw new Error("the page has no topbar or no sidebar");
    Object.assign(topbar.style, { position: "sticky", top: "0", zIndex: stacking });
    Object.assign(sidebar.style, { position: "sticky", top: "0", alignSelf: "flex-start", height: "100vh", overflowY: "hidden" });
  }, TOPBAR_STACKING);
}

export async function assertWhatIsOnScreen(page: Page, sceneId: string): Promise<void> {
  const pill = page.locator(".pui-sample-pill");
  const box = await pill.boundingBox();
  const onScreen = box !== null && box.y >= 0 && box.y + box.height <= VIEWPORT.height;
  if (!onScreen) throw new Error(`scene ${sceneId}: the "Sample data" pill is not on screen`);
  const text = await page.innerText("body");
  if (!text.includes("Harborline Supply Co. (fictional)")) throw new Error(`scene ${sceneId}: the company is not labelled fictional`);
  if (HOSTNAME.test(text)) throw new Error(`scene ${sceneId}: a hostname or address is in the page text`);
}

/** Scroll so `selector` sits just under the topbar, easing in and out so the motion reads as a hand. */
export async function scrollTo(page: Page, selector: string): Promise<void> {
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

/** The scenes of one recording, timed on the video's own clock. */
export class Timeline {
  readonly scenes: Scene[] = [];
  private videoStart = 0;
  private sceneStart = 0;

  /** Call right after the recording page exists: scene times are measured from here. */
  startClock(): void {
    this.videoStart = Date.now();
  }

  now(): number {
    return Date.now() - this.videoStart;
  }

  /** A scene starts when its page has settled, not when navigation began, so the edit never shows a
   * half-drawn page: settle() moves the mark. */
  markStart(): void {
    this.sceneStart = this.now();
  }

  /** Waits until the page is quiet and its fonts are in, pins the chrome, and marks the scene's start. */
  async settle(page: Page): Promise<void> {
    await page.waitForLoadState("networkidle");
    const states = await page.evaluate(() =>
      document.fonts.ready.then(() => [...document.fonts].map((face) => face.status)),
    );
    if (!states.includes("loaded") || states.includes("error")) throw new Error("the fonts did not load");
    await pinChrome(page);
    await page.waitForTimeout(SETTLE_MS);
    this.markStart();
  }

  /** Runs `body` as one scene, checks what is on screen at its end (the dashboard's checks unless the
   * scene is another page and brings its own), and records when it began and ended. */
  async scene(
    page: Page,
    meta: Pick<Scene, "id" | "caption"> & Partial<Pick<Scene, "speed" | "gif">>,
    body: () => Promise<void>,
    check: (page: Page, sceneId: string) => Promise<void> = assertWhatIsOnScreen,
  ): Promise<void> {
    this.markStart();
    await body();
    await check(page, meta.id);
    this.scenes.push({ speed: 1, gif: false, ...meta, startMs: this.sceneStart, endMs: this.now() });
  }
}
