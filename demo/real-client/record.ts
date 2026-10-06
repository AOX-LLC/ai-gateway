// Records the real-client clip: the transcript of one real Claude Code run through the gateway (played in a
// terminal page in the portfolio tokens), then the dashboard showing the layer that blocked a call.
//
//   THEME=light|dark KIND=realistic|compliant node real-client/record.ts
//
// Needs demo/out/real-client/render-<kind>.json (render.py, from a real run: run.sh) and the demo stack with
// that run's calls in its telemetry (take.sh does the order). The recording is of the transcript, a rendering
// of what the run recorded, and the clip says so in its label; it is not a screen capture of Claude Code,
// whose banner shows an account and a working directory.
//
// Like record.ts it checks what it films: "Sample data" and "fictional" on screen, no hostname, address or
// token in the page text, the dashboard's fonts loaded, no console error. check-frames.ts OCRs the result.
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";
import type { Browser, Page } from "playwright";
import { BASE_URL, HOSTNAME, Timeline, VIEWPORT, scrollTo, signInOffCamera } from "../dashboard.ts";
import { DEMO_DIR, OUT_DIR, REPO_DIR, parseTheme, readConfig, themeDir } from "../lib.ts";
import type { Theme } from "../lib.ts";

type Kind = "realistic" | "compliant";

interface Transcript {
  kind: Kind;
  header: string;
  label: string;
  tools: string[];
  withheld: string[];
  prompt: string;
  lines: { kind: string; text: string }[];
  answer: string[];
  answer_more: number;
  counts: { calls: number; blocked: number; answered: number };
}

const KIND_TITLES: Record<Kind, string> = { realistic: "Realistic task", compliant: "Compliant-model run" };
const TOKEN = /gw[\s_.-]?[A-Za-z0-9]{8,}/;

/** Pacing of the typed-out transcript, in recorded time. */
const HEADER_HOLD_MS = 2200;
const TOOLS_HOLD_MS = 2600;
const CALL_MS = 420;
const RESULT_MS = 380;
const BLOCKED_HOLD_MS = 2200;
const FOLDED_MS = 700;
const CLAUDE_MS = 900;
const ANSWER_HOLD_MS = 4200;
const DASHBOARD_PANEL_HOLD_MS = 4200;
const ROW_OUTLINE = "2px solid var(--pui-danger)";
const ROW_TINT = "var(--pui-danger-soft)";

const theme: Theme = parseTheme(process.env.THEME ?? "dark");
const kind = (process.env.KIND ?? "realistic") as Kind;
if (kind !== "realistic" && kind !== "compliant") throw new Error(`KIND must be realistic or compliant`);
const take = `real-${kind}`;
/** Only the short, realistic run is a GIF, and only its middle: the tools, the calls and the refused row.
 * A GIF cannot hold a long scrolling terminal under 4 MB. */
const gifRun = kind === "realistic";
const outDir = themeDir(theme, take);
const timeline = new Timeline();
const consoleErrors: string[] = [];

function readTranscript(): Transcript {
  return JSON.parse(readFileSync(join(OUT_DIR, "real-client", `render-${kind}.json`), "utf8")) as Transcript;
}

function terminalHtml(transcript: Transcript): string {
  const config = readConfig();
  const values: Record<string, string> = {
    theme,
    kind,
    kindTitle: KIND_TITLES[kind],
    label: transcript.label,
    fonts: pathToFileURL(join(REPO_DIR, "dashboard", "src", "fonts")).href,
    logo: pathToFileURL(resolve(DEMO_DIR, config.logos[theme])).href,
  };
  const template = readFileSync(join(DEMO_DIR, "real-client", "terminal.html"), "utf8");
  return template.replace(/\{\{(\w+)\}\}/g, (_, key: string) => {
    if (!(key in values)) throw new Error(`terminal.html uses an unknown placeholder: ${key}`);
    return values[key].replace(/&/g, "&amp;").replace(/</g, "&lt;");
  });
}

async function addLine(page: Page, className: string, text: string): Promise<void> {
  await page.evaluate(
    ([name, content]) => {
      const line = document.createElement("div");
      line.className = name;
      line.textContent = content;
      document.querySelector("#term")?.append(line);
    },
    [className, text],
  );
}

async function assertTerminalOnScreen(page: Page, sceneId: string): Promise<void> {
  const text = await page.innerText("body");
  if (!/sample data/i.test(text)) throw new Error(`scene ${sceneId}: no "Sample data" pill`);
  if (!text.includes("Harborline Supply Co. (fictional)")) throw new Error(`scene ${sceneId}: the company is not labelled fictional`);
  if (HOSTNAME.test(text) || TOKEN.test(text)) throw new Error(`scene ${sceneId}: a hostname, address or token is on screen`);
}

async function playTranscript(page: Page, transcript: Transcript): Promise<void> {
  await timeline.scene(
    page,
    { id: "prompt", gif: false, caption: transcript.label },
    async () => {
      await addLine(page, "dim", transcript.header);
      await page.waitForTimeout(HEADER_HOLD_MS / 2);
      await addLine(page, "prompt", `> ${transcript.prompt}`);
      await page.waitForTimeout(HEADER_HOLD_MS / 2);
    },
    assertTerminalOnScreen,
  );
  await timeline.scene(
    page,
    { id: "tools", gif: gifRun, caption: "The gateway offers this client only the tools it is scoped to" },
    async () => {
      await addLine(page, "dim", `\ntools the gateway offers this client (${transcript.tools.length}):`);
      await addLine(page, "call", transcript.tools.join("  "));
      if (transcript.withheld.length > 0) {
        await addLine(page, "dim", `held back from it: ${transcript.withheld.join("  ")}`);
      }
      await page.waitForTimeout(TOOLS_HOLD_MS);
    },
    assertTerminalOnScreen,
  );
  await timeline.scene(
    page,
    { id: "calls", gif: gifRun, caption: "Every call goes through the gateway" },
    async () => {
      await addLine(page, "dim", "");
      for (const line of transcript.lines) {
        await addLine(page, line.kind === "call" ? "call" : line.kind, line.kind === "call" ? `-> ${line.text}` : line.kind === "claude" ? line.text : `<- ${line.text}`);
        const pause = { call: CALL_MS, result: RESULT_MS, error: RESULT_MS, folded: FOLDED_MS, claude: CLAUDE_MS, blocked: BLOCKED_HOLD_MS }[line.kind] ?? CALL_MS;
        await page.waitForTimeout(pause);
      }
    },
    assertTerminalOnScreen,
  );
  await timeline.scene(
    page,
    { id: "answer", gif: false, caption: "What Claude told the user" },
    async () => {
      await addLine(page, "dim", "\nClaude:");
      for (const line of transcript.answer) await addLine(page, "answer", line);
      if (transcript.answer_more > 0) await addLine(page, "dim", `   (${transcript.answer_more} more lines)`);
      const { calls, blocked } = transcript.counts;
      await page.evaluate(
        ([summary, blockedCount]) => {
          const footer = document.querySelector("#summary");
          if (!footer) return;
          footer.textContent = summary as string;
          if (blockedCount) footer.classList.add("bad");
        },
        [`${calls} calls through the gateway · ${blocked} refused by it`, blocked],
      );
      await page.waitForTimeout(ANSWER_HOLD_MS);
    },
    assertTerminalOnScreen,
  );
}

/** The newest row the gateway refused, outlined and tinted so the eye finds the layer's name in its row. */
async function outlineBlockedRow(page: Page): Promise<void> {
  const rows = page.locator("#decisions tbody tr", { hasText: "Blocked" });
  for (let pageNumber = 0; pageNumber < 3 && (await rows.count()) === 0; pageNumber++) {
    await page.getByRole("button", { name: "Older" }).click();
    await page.waitForLoadState("networkidle");
  }
  const row = rows.first();
  if ((await row.count()) === 0) throw new Error("no blocked decision is on the first pages: did the run reach the gateway?");
  await row.evaluate(
    (element, [outline, tint]) => {
      Object.assign((element as HTMLElement).style, { outline, outlineOffset: "-2px", backgroundColor: tint });
      element.scrollIntoView({ block: "center" });
    },
    [ROW_OUTLINE, ROW_TINT],
  );
}

async function playDashboard(page: Page): Promise<void> {
  await timeline.scene(page, { id: "dashboard-layers", gif: false, caption: "The dashboard names the layer that stopped it" }, async () => {
    await page.goto(`${BASE_URL}/?range=1h`);
    await page.waitForSelector("#layers svg");
    await timeline.settle(page);
    await scrollTo(page, "#layers");
    await page.waitForTimeout(DASHBOARD_PANEL_HOLD_MS);
  });
  await timeline.scene(page, { id: "dashboard-decision", gif: gifRun, caption: "The refused call, with its layer and code" }, async () => {
    await scrollTo(page, "#decisions");
    await outlineBlockedRow(page);
    await page.waitForTimeout(DASHBOARD_PANEL_HOLD_MS);
  });
}

async function recordTake(browser: Browser, statePath: string, transcript: Transcript): Promise<void> {
  const context = await browser.newContext({
    viewport: VIEWPORT,
    colorScheme: theme,
    locale: "en-US",
    timezoneId: "UTC",
    reducedMotion: "reduce",
    storageState: statePath,
    recordVideo: { dir: join(outDir, "video-tmp"), size: VIEWPORT },
  });
  const page = await context.newPage();
  page.on("console", (message) => message.type() === "error" && consoleErrors.push(message.text().slice(0, 200)));
  page.on("pageerror", (error) => consoleErrors.push(String(error).slice(0, 200)));
  const htmlPath = join(outDir, "_terminal.html");
  writeFileSync(htmlPath, terminalHtml(transcript));
  await page.goto(pathToFileURL(htmlPath).href);
  await page.evaluate(() => document.fonts.ready);
  timeline.startClock();
  await playTranscript(page, transcript);
  await playDashboard(page);
  if (consoleErrors.length > 0) throw new Error(`the browser console shows errors: ${consoleErrors.slice(0, 3).join("; ")}`);
  const video = page.video();
  await context.close();
  await video?.saveAs(join(outDir, "raw.webm"));
}

async function main(): Promise<void> {
  const transcript = readTranscript();
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch();
  try {
    await recordTake(browser, await signInOffCamera(browser, theme, outDir), transcript);
  } finally {
    // The session cookie, the scratch video and the page file go whether or not the take worked.
    rmSync(join(outDir, "state.json"), { force: true });
    rmSync(join(outDir, "video-tmp"), { recursive: true, force: true });
    await browser.close();
  }
  writeFileSync(
    join(outDir, "timeline.json"),
    JSON.stringify({ project: "ai-gateway", theme, scenes: timeline.scenes }, null, 2),
  );
  console.log(`recorded ${timeline.scenes.length} scenes -> ${outDir}`);
}

main().catch((error) => {
  console.error(String(error instanceof Error ? error.message : error).split("\n")[0]);
  process.exit(1);
});
