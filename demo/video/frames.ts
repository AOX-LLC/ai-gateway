// Renders the still frames of the video: the title and end cards, the text slides and the SVG stills
// (the scorecard chart and the architecture diagram), in the portfolio dark theme at 1440x900.
import { mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";
import { DEMO_DIR, HEIGHT, OUT_DIR, REPO_DIR, WIDTH, readConfig } from "../lib.ts";

export const VIDEO_OUT = join(OUT_DIR, "video");
const FOOTER = "Sample data · Harborline Supply Co. is fictional";
const STILLS: Record<string, string> = {
  scorecard: join(REPO_DIR, "docs", "images", "scorecard.svg"),
  architecture: join(REPO_DIR, "docs", "images", "architecture-dark.svg"),
};
const END_COMMANDS = [
  "git clone https://github.com/AOX-LLC/ai-gateway",
  "scripts/run_dashboard_demo.sh up",
  "scripts/run_dashboard_demo.sh seed",
];

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function stat(figure: string, label: string, tone: "bad" | "good"): string {
  return `<div class="stat"><span class="stat-figure ${tone}">${escapeHtml(figure)}</span><span class="stat-label">${escapeHtml(label)}</span></div>`;
}

function titleBody(facts: Record<string, string>): string {
  const config = readConfig();
  return [
    `<p class="eyebrow">${escapeHtml(config.eyebrow)}</p>`,
    `<h1>${escapeHtml(config.title)}</h1>`,
    `<p class="subtitle">${escapeHtml(config.subtitle)}</p>`,
    `<div class="stats">${stat(`${facts.all_off} of ${facts.attacks}`, "attacks succeed with every layer off", "bad")}${stat(`${facts.all_on} of ${facts.attacks}`, "succeed with every layer on", "good")}</div>`,
  ].join("");
}

function endBody(): string {
  return [
    `<p class="eyebrow">Run it yourself</p>`,
    `<h2>No API key needed</h2>`,
    `<pre>${END_COMMANDS.map(escapeHtml).join("\n")}</pre>`,
    `<p class="repo">github.com/AOX-LLC/ai-gateway</p>`,
  ].join("");
}

function textBody(spec: string): string {
  const [heading, ...lines] = spec.split("/").map((part) => part.trim());
  return `<h2>${escapeHtml(heading)}</h2><ul>${lines.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`;
}

function stillBody(name: string): string {
  const path = STILLS[name];
  if (!path) throw new Error(`no still named ${name}: use ${Object.keys(STILLS).join(" or ")}`);
  return `<div class="figure"><img src="${pathToFileURL(path).href}" alt="${escapeHtml(name)}"></div>`;
}

function bodyFor(source: string, facts: Record<string, string>): string {
  const [kind, ...rest] = source.split(":");
  const argument = rest.join(":");
  if (kind === "card" && argument === "title") return titleBody(facts);
  if (kind === "card" && argument === "end") return endBody();
  if (kind === "text") return textBody(argument);
  if (kind === "still") return stillBody(argument);
  throw new Error(`not a frame source: ${source}`);
}

/** Renders one PNG per frame source (a source used twice is rendered once); returns source -> png path. */
export async function renderFrames(sources: string[], facts: Record<string, string>): Promise<Map<string, string>> {
  mkdirSync(VIDEO_OUT, { recursive: true });
  const config = readConfig();
  const template = readFileSync(join(DEMO_DIR, "video", "frame.html"), "utf8");
  const rendered = new Map<string, string>();
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: WIDTH, height: HEIGHT }, colorScheme: "dark" });
    let index = 0;
    for (const source of new Set(sources)) {
      const values: Record<string, string> = {
        title: "AI Gateway video frame",
        fonts: pathToFileURL(join(REPO_DIR, "dashboard", "src", "fonts")).href,
        logo: pathToFileURL(resolve(DEMO_DIR, config.logos.dark)).href,
        body: bodyFor(source, facts),
        footer: FOOTER,
      };
      const html = template.replace(/\{\{(\w+)\}\}/g, (_, key: string) => values[key] ?? "");
      // Written beside the PNG because file:// fonts and images do not load into an about:blank page.
      const htmlPath = join(VIDEO_OUT, `_frame-${index}.html`);
      writeFileSync(htmlPath, html);
      await page.goto(pathToFileURL(htmlPath).href);
      await page.evaluate(() => document.fonts.ready);
      await page.evaluate(() => Promise.all([...document.images].map((image) => image.decode().catch(() => undefined))));
      const png = join(VIDEO_OUT, `frame-${index}.png`);
      await page.screenshot({ path: png });
      rendered.set(source, png);
      index += 1;
    }
  } finally {
    await browser.close();
  }
  return rendered;
}
