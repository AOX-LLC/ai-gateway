// Reads docs/video-script.md: the timing table is the one source of the silent cuts, the captions and the
// teleprompter, so a change to the script regenerates all three.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { REPO_DIR, readJson } from "../lib.ts";

export type Cut = "core" | "extended";

export interface ScriptScene {
  id: string;
  cut: "core" | "ext";
  seconds: number;
  source: string;
  narration: string;
}

export interface Cue {
  start: number;
  end: number;
  text: string;
  scene: string;
}

export const SCRIPT_PATH = join(REPO_DIR, "docs", "video-script.md");
const TABLE_START = "<!-- script:start -->";
const TABLE_END = "<!-- script:end -->";
const COLUMNS = ["id", "cut", "seconds", "source", "narration"];

/** Narration starts a moment after its scene does and ends a moment before the next scene. */
const LEAD_IN_S = 0.4;
const TAIL_S = 0.4;
const CUE_GAP_S = 0.1;
export const WORDS_PER_SECOND_WARN = 2.7;
export const WORDS_PER_SECOND_MAX = 3.2;

interface Scorecard {
  deterministic: {
    attacks: { hostile: boolean }[];
    columns: unknown[];
    summary: { per_column: Record<string, { succeeded: number; hostile: number }> };
  };
}

/** The numbers the narration may quote, read from docs/scorecard.json when the build runs. */
export function loadFacts(): Record<string, string> {
  const { deterministic } = readJson<Scorecard>(join(REPO_DIR, "docs", "scorecard.json"));
  const column = deterministic.summary.per_column;
  return {
    attacks: String(column["all-on"].hostile),
    columns: String(deterministic.columns.length),
    all_on: String(column["all-on"].succeeded),
    all_off: String(column["all-off"].succeeded),
  };
}

export function fillFacts(text: string, facts: Record<string, string>): string {
  return text.replace(/\{\{(\w+)\}\}/g, (_, key: string) => {
    if (!(key in facts)) throw new Error(`the script uses an unknown fact: {{${key}}}`);
    return facts[key];
  });
}

export function parseScript(markdown: string): ScriptScene[] {
  const start = markdown.indexOf(TABLE_START);
  const end = markdown.indexOf(TABLE_END);
  if (start === -1 || end === -1 || end < start) throw new Error(`docs/video-script.md has no ${TABLE_START} table`);
  const rows = markdown
    .slice(start + TABLE_START.length, end)
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith("|"));
  const [header, , ...body] = rows;
  const names = cells(header);
  if (names.join(",") !== COLUMNS.join(",")) throw new Error(`the table's columns must be ${COLUMNS.join(", ")}`);
  const ids = new Set<string>();
  return body.map((row) => {
    const [id, cut, seconds, source, narration] = cells(row);
    if (cut !== "core" && cut !== "ext") throw new Error(`scene ${id}: cut must be core or ext`);
    if (ids.has(id)) throw new Error(`scene ${id} appears twice`);
    ids.add(id);
    const length = Number(seconds);
    if (!(length > 0)) throw new Error(`scene ${id}: seconds must be a positive number`);
    return { id, cut, seconds: length, source, narration };
  });
}

/** A row's cells; a "|" inside a cell is not allowed, so a plain split is enough. */
function cells(row: string): string[] {
  return row.replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

export function loadScript(): ScriptScene[] {
  return parseScript(readFileSync(SCRIPT_PATH, "utf8"));
}

export function scenesOf(scenes: ScriptScene[], cut: Cut): ScriptScene[] {
  return cut === "extended" ? scenes : scenes.filter((scene) => scene.cut === "core");
}

export function sentences(narration: string): string[] {
  return narration.split(/(?<=[.?!])\s+/).map((sentence) => sentence.trim()).filter(Boolean);
}

export function wordCount(text: string): number {
  return text.split(/\s+/).filter(Boolean).length;
}

export function wordsPerSecond(scene: ScriptScene, facts: Record<string, string>): number {
  const spoken = Math.max(scene.seconds - LEAD_IN_S - TAIL_S, 0.1);
  return wordCount(fillFacts(scene.narration, facts)) / spoken;
}

/** One cue per sentence, each given a share of its scene's speaking time in proportion to its words. */
export function cuesFor(scenes: ScriptScene[], facts: Record<string, string>): Cue[] {
  const cues: Cue[] = [];
  let sceneStart = 0;
  for (const scene of scenes) {
    const lines = sentences(fillFacts(scene.narration, facts));
    const total = lines.reduce((sum, line) => sum + wordCount(line), 0);
    const speaking = scene.seconds - LEAD_IN_S - TAIL_S;
    let cursor = sceneStart + LEAD_IN_S;
    for (const line of lines) {
      const share = (speaking * wordCount(line)) / total;
      cues.push({ start: cursor, end: cursor + share - CUE_GAP_S, text: line, scene: scene.id });
      cursor += share;
    }
    sceneStart += scene.seconds;
  }
  return cues;
}

export function totalSeconds(scenes: ScriptScene[]): number {
  return scenes.reduce((sum, scene) => sum + scene.seconds, 0);
}
