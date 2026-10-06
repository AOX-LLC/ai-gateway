// Media hygiene check: OCRs frames of every published video, gif and png and flags text that must not ship,
// and checks that the frames that must say "Sample data" do.
// Usage: node check-frames.ts [--reviewed] <paths...> | --all | --selftest
// Exit code is 1 when anything is flagged. Contact sheets for a human eyeball land in out/contact/.
//
// Flagged: any IPv4 address (127.0.0.1 too: a clip has no reason to show an address), any hostname or URL
// other than the project's own repository, `localhost`, a shell prompt or home path, an e-mail address, a
// gateway token (`gw_` and a run of letters or digits), and every line of the private denylist
// (.denylist.local, git-ignored: names of internal machines and tools; its matches are never printed).
// `--reviewed` is for a person who has read the findings (and the frames they came from) and judged them
// false positives, which small palette-reduced GIF text produces. It accepts findings in .gif files only, and
// never a denylist hit (its line is withheld, so nobody can have read it), a gateway token or a missing
// "Sample data": those fail the run whatever the flag. Accepted findings still print. Never pass it to get
// past a finding that has not been read.
// Required: files whose name matches config.check.requiredTextPattern must show the required text in every
// sampled frame (the "Sample data" pill). A video is sampled at one frame a second, plus its last; a frame
// that repeats the one before it is read once.
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, extname, join, relative, resolve } from "node:path";
import { promisify } from "node:util";
import { chromium } from "playwright";
import { IMAGES_DIR, MEDIA_DIR, OUT_DIR, REPO_DIR, ffmpeg, probeWidth, readConfig } from "./lib.ts";

const execFileAsync = promisify(execFile);

/** Rules that no review can accept: the person could not read the line, or the finding is a secret. */
const NEVER_ACCEPTED_RULES = ["denylist", "gateway token", "required text"];

const MEDIA_EXTENSIONS = [".mp4", ".gif", ".webm", ".png"];
const SKIPPED_DIRECTORIES = ["contact", ".work", "video-tmp", ".selftest", "node_modules"];
const PARALLEL_OCR = 4;
const SHEET_COLUMNS = 6;
const SHEET_ROWS = 4;
const OCR_SCALE = 2;
const OCR_UPSCALE_BELOW_WIDTH = 2000;
const TOP_BAND_FRACTION = 0.1;
const TOP_BAND_SCALE = 3;

const IPV4 = /\b(?:\d{1,3}\.){3}\d{1,3}\b/g;
const TLDS = "com|net|org|io|dev|ai|app|co|us|uk|de|local|lan|internal|home|arpa|xyz|info|biz|cloud|me|tv|ly|sh|gg|cc|to|so|run|page|site|online|tech";
const HOSTNAME = new RegExp(
  `\\b(?:https?:\\/\\/)?(?:[a-z0-9-]+\\.)+(?:${TLDS})\\b(?::\\d+)?(?:\\/[^\\s"'<>)]*)?`, "gi",
);
const URL_WITH_SCHEME = /\bhttps?:\/\/[^\s"'<>)]+/gi;
const LOCALHOST = /\blocalhost\b/i;
const EMAIL = /[\w.+-]+@[\w-]+(?:\.[\w-]+)+/;
// OCR often drops an underscore, so `gw_AbCd...` can be read as `gw AbCd...`: allow a separator or none.
const TOKEN = /\bgw[\s_.\-]?[A-Za-z0-9]{8,}\b/;
// Only `$ `: a line starting with `# ` or `%` is mostly UI text and icons that OCR reads wrongly.
const SHELL_PROMPT_START = /^\$ /;
const SHELL_PROMPT_CONTAINS = /user@host|~\/|\/home\/|\/Users\//;

export interface Finding {
  file: string;
  time: string;
  rule: string;
  line: string;
}

interface Frame {
  time: string;
  path: string;
}

function extraTerms(): string[] {
  return (process.env.CHECK_EXTRA_TERMS ?? "").split(",").map((term) => term.trim().toLowerCase()).filter(Boolean);
}

function loadDenylist(): string[] {
  const path = join(REPO_DIR, ".denylist.local");
  if (!existsSync(path)) {
    if (process.env.CHECK_NO_DENYLIST === "1") return extraTerms();
    throw new Error(
      "no .denylist.local: without it the internal-name check checks nothing. Copy it from the main checkout, or set CHECK_NO_DENYLIST=1 to run without it.",
    );
  }
  return readFileSync(path, "utf8")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "" && !line.startsWith("#"))
    .map((line) => line.toLowerCase())
    .concat(extraTerms());
}

function isAllowedHost(token: string, allowedRepo: RegExp): boolean {
  return allowedRepo.test(token);
}

function addressRules(line: string, allowedRepo: RegExp): string[] {
  const rules: string[] = [];
  if (IPV4.test(line)) rules.push("IPv4 address");
  IPV4.lastIndex = 0;
  if (LOCALHOST.test(line)) rules.push("localhost");
  const hosts = [...(line.match(HOSTNAME) ?? []), ...(line.match(URL_WITH_SCHEME) ?? [])];
  if (hosts.some((token) => !isAllowedHost(token, allowedRepo))) rules.push("hostname or URL");
  return rules;
}

function promptRules(line: string): string[] {
  const text = line.trim();
  return SHELL_PROMPT_START.test(text) || SHELL_PROMPT_CONTAINS.test(text) ? ["shell prompt or path"] : [];
}

function secretRules(line: string): string[] {
  return [...(EMAIL.test(line) ? ["e-mail address"] : []), ...(TOKEN.test(line) ? ["gateway token"] : [])];
}

function denylistRules(line: string, denylist: string[]): string[] {
  const lower = line.toLowerCase();
  return denylist.flatMap((term, index) => (lower.includes(term) ? [`denylist term #${index + 1}`] : []));
}

export function judgeLine(line: string, allowedRepo: RegExp, denylist: string[]): string[] {
  return [
    ...addressRules(line, allowedRepo), ...promptRules(line), ...secretRules(line), ...denylistRules(line, denylist),
  ];
}

async function tesseract(image: string, mode: string): Promise<string[]> {
  const { stdout } = await execFileAsync("tesseract", [image, "-", "--psm", mode], {
    env: { ...process.env, OMP_THREAD_LIMIT: "1" },
    maxBuffer: 32 * 1024 * 1024,
  });
  return stdout.split("\n").map((line) => line.trim()).filter((line) => line !== "");
}

async function ocrLines(image: string): Promise<string[]> {
  return [...new Set([...(await tesseract(image, "11")), ...(await tesseract(image, "6"))])];
}

/** The top band, enlarged: the pill is small mono capitals that the whole-frame read sometimes misses. */
async function topBandLines(image: string, work: string): Promise<string[]> {
  const band = join(work, `band-${basename(image)}`);
  ffmpeg(["-i", image, "-vf", `crop=iw:ih*${TOP_BAND_FRACTION}:0:0,scale=iw*${TOP_BAND_SCALE}:ih*${TOP_BAND_SCALE}:flags=lanczos`, band]);
  return tesseract(band, "11");
}

async function mapLimit<T, R>(items: T[], limit: number, task: (item: T) => Promise<R>): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let next = 0;
  async function worker(): Promise<void> {
    while (next < items.length) {
      const index = next++;
      results[index] = await task(items[index]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

const upscale = `scale=iw*${OCR_SCALE}:ih*${OCR_SCALE}:flags=lanczos`;

function extractPngFrame(file: string, work: string): Frame[] {
  const path = join(work, "png-frame.png");
  // A 2x screenshot is already large enough to read; a full-page one would be enormous if enlarged again.
  ffmpeg(["-i", file, "-vf", probeWidth(file) < OCR_UPSCALE_BELOW_WIDTH ? upscale : "null", path]);
  return [{ time: "image", path }];
}

// One frame per second, plus the last.
function extractVideoFrames(file: string, work: string): Frame[] {
  ffmpeg(["-i", file, "-vf", `fps=1,${upscale}`, join(work, "f%04d.png")]);
  const frames = readdirSync(work)
    .filter((name) => /^f\d{4}\.png$/.test(name))
    .sort()
    .map((name, index) => ({ time: `${index}s`, path: join(work, name) }));
  const lastPath = join(work, "last.png");
  try {
    ffmpeg(["-sseof", "-0.2", "-i", file, "-vf", upscale, "-update", "1", "-frames:v", "1", lastPath]);
    if (existsSync(lastPath)) frames.push({ time: "last", path: lastPath });
  } catch {
    // Some webm files carry no duration, so seeking from the end is impossible; the 1 fps frames still cover the clip.
  }
  return frames;
}

function dropRepeatedFrames(frames: Frame[]): Frame[] {
  let previous = "";
  return frames.filter((frame) => {
    const hash = createHash("md5").update(readFileSync(frame.path)).digest("hex");
    const repeated = hash === previous;
    previous = hash;
    return !repeated;
  });
}

function contactSheetPath(file: string): string {
  const dir = join(OUT_DIR, "contact");
  mkdirSync(dir, { recursive: true });
  return join(dir, `${relative(REPO_DIR, file).replace(/[/.]/g, "-")}.png`);
}

function writeContactSheet(file: string, frames: Frame[], work: string): void {
  const capacity = SHEET_COLUMNS * SHEET_ROWS;
  const step = Math.max(1, frames.length / capacity);
  const chosen = Array.from({ length: Math.min(capacity, frames.length) }, (_, i) => frames[Math.floor(i * step)]);
  const sheetDir = join(work, "sheet");
  mkdirSync(sheetDir);
  chosen.forEach((frame, i) => copyFileSync(frame.path, join(sheetDir, `s${String(i).padStart(3, "0")}.png`)));
  ffmpeg([
    "-i", join(sheetDir, "s%03d.png"), "-vf",
    `scale=320:-1,tile=${SHEET_COLUMNS}x${SHEET_ROWS}:padding=4:color=black`, "-frames:v", "1", contactSheetPath(file),
  ]);
}

async function frameSaysRequiredText(frame: Frame, lines: string[], required: string, work: string): Promise<boolean> {
  if (lines.join(" ").toLowerCase().includes(required)) return true;
  return (await topBandLines(frame.path, work)).join(" ").toLowerCase().includes(required);
}

export async function checkFile(file: string, options: { sheet: boolean } = { sheet: true }): Promise<Finding[]> {
  const config = readConfig();
  const allowedRepo = new RegExp(config.check.allowedRepoPattern, "i");
  const mustSayRequired = new RegExp(config.check.requiredTextPattern, "i").test(basename(file));
  const denylist = loadDenylist();
  const work = mkdtempSync(join(tmpdir(), "check-frames-"));
  try {
    const isImage = extname(file).toLowerCase() === ".png";
    const frames = isImage ? extractPngFrame(file, work) : extractVideoFrames(file, work);
    if (options.sheet) writeContactSheet(file, frames, work);
    const found: Finding[] = [];
    const perFrame = await mapLimit(dropRepeatedFrames(frames), PARALLEL_OCR, async (frame) => ({
      frame, lines: await ocrLines(frame.path),
    }));
    for (const { frame, lines } of perFrame) {
      for (const line of lines) {
        for (const rule of judgeLine(line, allowedRepo, denylist)) {
          // The denylist is private, so its matches never print the OCR line.
          const shown = rule.startsWith("denylist") ? "(withheld)" : line;
          found.push({ file: relative(REPO_DIR, file), time: frame.time, rule, line: shown });
        }
      }
      if (mustSayRequired && !(await frameSaysRequiredText(frame, lines, config.check.requiredText, work))) {
        found.push({ file: relative(REPO_DIR, file), time: frame.time, rule: "required text missing", line: config.check.requiredText });
      }
    }
    return found;
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

function walk(dir: string): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return SKIPPED_DIRECTORIES.includes(name) ? [] : walk(path);
    return MEDIA_EXTENSIONS.includes(extname(name).toLowerCase()) ? [path] : [];
  });
}

function printFindings(findings: Finding[]): void {
  const clip = (text: string) => (text.length > 90 ? `${text.slice(0, 87)}...` : text);
  const rows = findings.map((f) => [f.file, f.time, f.rule, clip(f.line)]);
  const header = ["file", "frame", "rule", "ocr line"];
  const widths = header.map((h, i) => Math.max(h.length, ...rows.map((row) => row[i].length)));
  const format = (row: string[]) => row.map((cell, i) => cell.padEnd(widths[i])).join("  ");
  console.log([format(header), ...rows.map(format)].join("\n"));
}

async function renderTextPng(path: string, text: string): Promise<void> {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 300 } });
  await page.setContent(
    `<body style="margin:0;background:#fff;color:#000;font:48px 'DejaVu Sans Mono',monospace;padding:80px">${text}</body>`,
  );
  await page.screenshot({ path });
  await browser.close();
}

/** The check must catch what it is for and pass what it should: one dirty image, one clean one. */
async function selfTest(): Promise<void> {
  const dir = join(OUT_DIR, ".selftest");
  mkdirSync(dir, { recursive: true });
  const dirty = join(dir, "dirty.png");
  const clean = join(dir, "clean.png");
  process.env.CHECK_EXTRA_TERMS = "zzzqwerty";
  process.env.CHECK_NO_DENYLIST = existsSync(join(REPO_DIR, ".denylist.local")) ? "0" : "1";
  await renderTextPng(dirty, "user@host:~$ ssh 100.64.0.1 zzzqwerty gw_AbCd1234Efgh mail me@example.org");
  await renderTextPng(clean, "Sample data for Harborline at github.com/AOX-LLC/ai-gateway");
  const dirtyRules = new Set((await checkFile(dirty, { sheet: false })).map((f) => f.rule));
  const cleanFindings = await checkFile(clean, { sheet: false });
  const expected = ["IPv4 address", "shell prompt or path", "gateway token", "e-mail address"];
  const termHit = [...dirtyRules].some((rule) => rule.startsWith("denylist term"));
  if (!termHit) throw new Error("selftest: the extra denylist term was not flagged");
  const missing = expected.filter((rule) => !dirtyRules.has(rule));
  if (missing.length > 0) throw new Error(`selftest: the dirty image did not trigger: ${missing.join(", ")}`);
  if (cleanFindings.length > 0) {
    printFindings(cleanFindings);
    throw new Error("selftest: the clean image was flagged");
  }
  console.log("selftest passed: the dirty image was flagged by every rule it should trip; the clean image passed");
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.includes("--selftest")) return selfTest();
  const reviewed = args.includes("--reviewed");
  const paths = args.filter((arg) => !arg.startsWith("--"));
  const files = args.includes("--all")
    ? [...walk(OUT_DIR), ...walk(MEDIA_DIR), ...walk(IMAGES_DIR)]
    : paths.map((arg) => resolve(arg));
  if (files.length === 0) throw new Error("usage: node check-frames.ts <paths...> | --all | --selftest");
  const findings: Finding[] = [];
  for (const file of files) {
    const fileFindings = await checkFile(file);
    console.log(`${fileFindings.length === 0 ? "clean  " : "FLAGGED"} ${relative(REPO_DIR, file)}`);
    findings.push(...fileFindings);
  }
  if (findings.length > 0) {
    console.log("");
    printFindings(findings);
    const accepted = (finding: Finding): boolean =>
      reviewed && finding.file.endsWith(".gif") && !NEVER_ACCEPTED_RULES.some((rule) => finding.rule.startsWith(rule));
    const refused = findings.filter((finding) => !accepted(finding));
    if (refused.length > 0) process.exitCode = 1;
    else console.log("\nThese findings, all in GIFs, were read by a person and accepted (--reviewed).");
  }
}

if (import.meta.main) await main();
