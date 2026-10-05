// Renders the 1280x640 social preview from templates/card.html. The two figures are read from
// docs/scorecard.json when it renders, so the card cannot disagree with the scorecard.
// Usage: node cards.ts [light|dark]   Output: out/social-<theme>.png (dark by default)
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";
import { DEMO_DIR, OUT_DIR, REPO_DIR, parseTheme, readConfig, readJson } from "./lib.ts";
import type { Theme } from "./lib.ts";

const SIZE = { width: 1280, height: 640 };
const FONTS_DIR = join(REPO_DIR, "dashboard", "src", "fonts");

interface ColumnSummary {
  hostile: number;
  succeeded: number;
}

/** "5 of 31" for a scorecard column: how many of the hostile attacks succeed in it. */
function scorecardFigure(columnId: string): string {
  const scorecard = readJson<{ deterministic: { summary: { per_column: Record<string, ColumnSummary> } } }>(
    join(REPO_DIR, "docs", "scorecard.json"),
  );
  const column = scorecard.deterministic.summary.per_column[columnId];
  if (!column) throw new Error(`docs/scorecard.json has no column ${columnId}`);
  return `${column.succeeded} of ${column.hostile}`;
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function renderHtml(theme: Theme): string {
  const config = readConfig();
  const values: Record<string, string> = {
    theme,
    width: String(SIZE.width),
    height: String(SIZE.height),
    fonts: pathToFileURL(FONTS_DIR).href,
    logo: pathToFileURL(resolve(DEMO_DIR, config.logos[theme])).href,
    eyebrow: escapeHtml(config.eyebrow),
    title: escapeHtml(config.title),
    subtitle: escapeHtml(config.subtitle),
    sampleLabel: escapeHtml(config.sampleLabel),
    repoUrl: escapeHtml(config.repoUrl),
    statOff: escapeHtml(scorecardFigure("all-off")),
    statOn: escapeHtml(scorecardFigure("all-on")),
  };
  const template = readFileSync(join(DEMO_DIR, "templates", "card.html"), "utf8");
  return template.replace(/\{\{(\w+)\}\}/g, (_, key: string) => {
    if (!(key in values)) throw new Error(`card.html uses an unknown placeholder: ${key}`);
    return values[key];
  });
}

async function renderSocial(theme: Theme): Promise<string> {
  mkdirSync(OUT_DIR, { recursive: true });
  // Written next to the PNG because file:// fonts do not load into an about:blank page.
  const htmlPath = join(OUT_DIR, `_social-${theme}.html`);
  writeFileSync(htmlPath, renderHtml(theme));
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: SIZE });
    await page.goto(pathToFileURL(htmlPath).href);
    await page.evaluate(() => document.fonts.ready);
    const png = join(OUT_DIR, `social-${theme}.png`);
    await page.screenshot({ path: png });
    return png;
  } finally {
    await browser.close();
  }
}

console.log(await renderSocial(process.argv[2] ? parseTheme(process.argv[2]) : "dark"));
